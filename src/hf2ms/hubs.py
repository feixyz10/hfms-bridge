"""Narrow REST adapters. No Hub SDK download caches or payload temp files."""

import logging
import re
from dataclasses import dataclass
from urllib.parse import quote, urlparse

from .http import (
    HTTP,
    HTTPError,
    RetryableTransferError,
    TransferError,
    check,
    error_stage,
    origin,
    payload,
    trusted_api_redirect,
    validate_url,
)


def repo_name(value):
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", value, flags=re.ASCII) or any(
        part in (".", "..") for part in value.split("/")
    ):
        raise ValueError("Repository must be owner/name")
    return value


@dataclass(frozen=True)
class File:
    path: str
    size: int
    sha256: str = ""

    def __post_init__(self):
        if (
            not self.path
            or self.path.startswith("/")
            or "\\" in self.path
            or any(p in ("", ".", "..") for p in self.path.split("/"))
        ):
            raise ValueError("Invalid repository file path")
        if self.size < 0:
            raise ValueError("Negative file size")
        if self.sha256 and not re.fullmatch("[0-9a-f]{64}", self.sha256):
            raise ValueError("Invalid SHA-256 metadata")


class HuggingFace:
    def __init__(self, repo, repo_type, token=None, endpoint="https://huggingface.co", timeout=120):
        self.repo = repo_name(repo)
        self.repo_type = repo_type
        self.token = token
        self.endpoint = validate_url(endpoint)
        self.http = HTTP(timeout)
        self.info = {}

    def close(self):
        self.http.close()

    def _get(self, url, **kwargs):
        headers = {"Authorization": "Bearer " + self.token} if self.token else {}
        return self.http.api_request("GET", url, headers=headers, **kwargs)

    def resolve(self, revision):
        url = "{}/api/{}s/{}/revision/{}".format(
            self.endpoint, self.repo_type, self.repo, quote(revision, safe="")
        )
        with self._get(url) as response:
            commit = payload(response)["sha"]
        if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise TransferError("HF did not return an immutable commit hash")
        return commit

    def detect(self):
        """Resolve a unique readable type; HF can return 401 for a missing namespace."""
        kinds = ("model", "dataset") if self.repo_type == "auto" else (self.repo_type,)
        found = []
        denied = []
        for kind in kinds:
            try:
                with self._get(
                    f"{self.endpoint}/api/{kind}s/{self.repo}", allow_redirects=False
                ) as response:
                    info = payload(response)
                found.append((kind, info))
            except HTTPError as exc:
                if exc.status in (401, 403):
                    denied.append(exc)
                elif exc.status != 404:
                    raise
        if not found:
            if denied:
                raise denied[0]
            raise TransferError("HF repository not found or not accessible with this token")
        if len(found) != 1:
            raise TransferError("Both model and dataset exist; specify --repo-type")
        self.repo_type, self.info = found[0]
        return self.repo_type

    def files(self, commit):
        url = "{}/api/{}s/{}/tree/{}?recursive=true&expand=false".format(
            self.endpoint, self.repo_type, self.repo, commit
        )
        visited = set()
        while url:
            if not trusted_api_redirect(self.endpoint, url) or url in visited:
                raise TransferError("Unsafe or cyclic HF pagination link")
            visited.add(url)
            with self._get(url, allow_redirects=False) as response:
                entries = payload(response)
                next_url = response.links.get("next", {}).get("url")
            for item in entries:
                if item.get("type") == "file":
                    lfs = item.get("lfs") or {}
                    yield File(item["path"], item["size"], lfs.get("oid", ""))
            url = next_url

    def open(self, file, commit, offset=0):
        prefix = "datasets/" if self.repo_type == "dataset" else ""
        url = "{}/{}{}/resolve/{}/{}".format(
            self.endpoint, prefix, self.repo, commit, quote(file.path, safe="/")
        )
        response = self.http.download(url, self.token, offset=offset, total=file.size)
        length = response.headers.get("Content-Length")
        if length is not None and int(length) != file.size - offset:
            response.close()
            raise TransferError("Source Content-Length disagrees with pinned metadata")
        return response


class ModelScope:
    def __init__(
        self,
        repo,
        repo_type,
        token,
        revision="master",
        endpoint="https://modelscope.cn",
        timeout=120,
    ):
        self.repo = repo_name(repo)
        self.repo_type, self.revision = repo_type, revision
        self.endpoint = validate_url(endpoint)
        self.token = token
        self.http = HTTP(timeout)
        self.blob_http = HTTP(timeout)
        self._cached_root = None
        self._cached_entries = {}

    def close(self):
        self.http.close()
        self.blob_http.close()

    def _api(self, method, path, **kwargs):
        headers = {"Authorization": "Bearer " + self.token, "Cookie": "m_session_id=" + self.token}
        try:
            with self.http.api_request(
                method,
                self.endpoint + "/api/v1/" + path,
                headers=headers,
                allow_redirects=False,
                **kwargs,
            ) as response:
                return payload(response)
        except HTTPError as exc:
            # Use the original API route, never a signed redirect/upload URL.
            context = f"{method} {urlparse(self.endpoint).hostname}/api/v1/{path}"
            raise HTTPError(exc.status, exc.retry_after, context=context) from exc

    def repo_exists(self):
        try:
            self._api("GET", f"{self.repo_type}s/{self.repo}")
            return True
        except HTTPError as exc:
            if exc.status == 404:
                return False
            raise

    def create_repo(self, visibility, license_name):
        owner, name = self.repo.split("/")
        body = {
            "Name": name,
            "Visibility": 1 if visibility == "private" else 5,
            "License": license_name,
        }
        body["Owner" if self.repo_type == "dataset" else "Path"] = owner
        kwargs = (
            {"files": {k: (None, str(v)) for k, v in body.items()}}
            if self.repo_type == "dataset"
            else {"json": body}
        )
        try:
            self._api("POST", f"{self.repo_type}s", **kwargs)
        except HTTPError as exc:
            # Another process may have created the same repository meanwhile.
            if exc.status != 409 or not self.repo_exists():
                raise

    def check_revision(self):
        try:
            result = self._api("GET", f"{self.repo_type}s/{self.repo}/revisions")
        except HTTPError as exc:
            if self.repo_type != "dataset" or exc.status != 404:
                raise
            if self.revision != "master":
                raise TransferError(
                    "MS dataset revision listing is unavailable (404); cannot confirm "
                    "a writable custom branch: " + self.revision
                ) from exc
            # Some dataset deployments expose the tree but no revision listing.
            # Only the conventional default branch gets this compatibility path;
            # a tree read alone cannot distinguish an arbitrary branch from a tag.
            result = self._api(
                "GET",
                f"datasets/{self.repo}/repo/tree",
                params={
                    "Revision": "master",
                    "Root": "/",
                    "Recursive": "False",
                    "PageNumber": 1,
                    "PageSize": 1,
                },
            )
            entries = (
                result.get("Files", result.get("files")) if isinstance(result, dict) else result
            )
            if not isinstance(entries, list):
                raise TransferError("Cannot confirm MS dataset master from file listing") from exc
            logging.getLogger("hf2ms").warning(
                "MS dataset revision listing unavailable (404); "
                "confirmed master via file listing; write permission will be checked on commit"
            )
            return
        if not isinstance(result, dict) or not isinstance(result.get("RevisionMap"), dict):
            raise TransferError("Cannot confirm writable MS branch from revision response")
        branches = result["RevisionMap"].get("Branches") or []
        names = [b.get("Revision") if isinstance(b, dict) else b for b in branches]
        if not names and not result["RevisionMap"].get("Tags") and self.revision == "master":
            return  # Empty repository: the first commit initializes the default branch.
        if self.revision not in names:
            raise TransferError(
                "MS branch does not exist or is not writable; create it first: " + self.revision
            )

    def file(self, path):
        """Read current destination metadata. Cache at most one directory (3000 files).

        Truncated model directories are rejected instead of treating unseen paths
        as absent. Large dataset directories remain paginated, with no full-tree
        allocation; an uncached lookup scans the pages again.
        """
        root = path.rpartition("/")[0]
        if root == self._cached_root:
            return self._cached_entries.get(path)
        entries = {}
        found = None
        page = 1
        previous = None
        count = 0
        while True:
            params = {
                "Revision": self.revision,
                "Root": root or "/",
                "Recursive": "True" if self.repo_type == "dataset" else "False",
            }
            if self.repo_type == "dataset":
                params.update(PageNumber=page, PageSize=200)
            suffix = "tree" if self.repo_type == "dataset" else "files"
            result = self._api("GET", f"{self.repo_type}s/{self.repo}/repo/{suffix}", params=params)
            if isinstance(result, dict):
                result = result.get("Files", result.get("files"))
            if not isinstance(result, list):
                raise TransferError("Unrecognized MS file listing; refusing to assume empty repo")
            if self.repo_type == "model" and len(result) >= 3000:
                raise TransferError("MS directory listing may be truncated at 3000 entries")
            signature = tuple(e.get("Path", e.get("path")) for e in result)
            if result and signature == previous:
                raise TransferError("MS dataset pagination did not advance")
            previous = signature
            for entry in result:
                name = entry.get("Path", entry.get("path"))
                if not name or name.rpartition("/")[0] != root:
                    continue
                count += 1
                if entry.get("Type", entry.get("type")) == "tree":
                    value = {"directory": True}
                else:
                    digest = entry.get("Sha256", entry.get("sha256", "")) or ""
                    if digest and not re.fullmatch("[0-9a-f]{64}", digest):
                        digest = ""  # never mistake a Git/Xet object ID for a file digest
                    value = {"size": entry.get("Size", entry.get("size")), "sha256": digest}
                if name == path:
                    found = value
                if count <= 3000:
                    entries[name] = value
            if self.repo_type != "dataset" or len(result) < 200:
                break
            page += 1
        if count <= 3000:
            self._cached_root, self._cached_entries = root, entries
        return found

    def negotiate(self, sha256, size):
        result = self._api(
            "POST",
            "repos/{}s/{}/info/lfs/objects/batch".format(self.repo_type, self.repo),
            json={"operation": "upload", "objects": [{"oid": sha256, "size": size}]},
        )
        objects = result.get("objects") if isinstance(result, dict) else None
        if not isinstance(objects, list) or any(not isinstance(o, dict) for o in objects):
            raise RetryableTransferError("LFS batch response has no valid objects list")
        # The legacy MS API lists blobs needing upload, not every requested OID.
        # An explicit empty list means reuse (also how the official SDK handles
        # _validate_blob). The file is only complete after commit succeeds.
        if not objects:
            return None
        matches = [o for o in objects if o.get("oid") == sha256]
        if len(matches) != 1:
            raise RetryableTransferError(
                f"LFS batch response: {len(objects)} object(s), "
                f"{len(matches)} matching requested SHA-256 (expected exactly 1)"
            )
        obj = matches[0]
        if obj.get("error"):
            raise TransferError("ModelScope rejected the LFS blob")
        if obj.get("size", size) != size:
            raise TransferError("LFS batch size mismatch")
        actions = obj.get("actions") or {}
        if "upload" not in actions:
            if actions:
                raise TransferError("Unsupported LFS transfer actions")
            return None
        action = actions["upload"]
        if not isinstance(action, dict) or not action.get("href"):
            raise TransferError("Invalid LFS upload action")
        validate_url(action["href"])
        return actions

    def _action_headers(self, action):
        url = action["href"]
        validate_url(url)
        headers = dict(action.get("header") or {})
        host = urlparse(url).hostname or ""
        endpoint_host = urlparse(self.endpoint).hostname or ""
        trusted = origin(url) == origin(self.endpoint)
        for domain in ("modelscope.cn", "modelscope.ai"):
            if (endpoint_host == domain or endpoint_host.endswith("." + domain)) and (
                host == domain or host.endswith("." + domain)
            ):
                trusted = True
        if trusted:
            headers.setdefault("Authorization", "Bearer " + self.token)
            headers.setdefault("Cookie", "m_session_id=" + self.token)
        return headers

    def upload(self, actions, stream, sha256, size):
        action = actions["upload"]
        headers = self._action_headers(action)
        headers["Content-Length"] = str(size)
        headers.pop("Transfer-Encoding", None)
        # Redirect replay is unsafe for a non-seekable stream. Renegotiate on failure.
        with self.blob_http.request(
            "PUT", action["href"], data=stream, headers=headers, allow_redirects=False
        ) as response:
            check(response)
            if response.content:
                payload(response)
        stream.verify()
        if "verify" in actions:
            action = actions["verify"]
            with error_stage("MS verify"):
                with self.blob_http.request(
                    "POST",
                    action["href"],
                    headers=self._action_headers(action),
                    json={"oid": sha256, "size": size},
                    allow_redirects=False,
                ) as response:
                    check(response)
                    if response.content:
                        payload(response)

    def commit(self, operation):
        return self._api(
            "POST",
            "repos/{}s/{}/commit/{}".format(
                self.repo_type, self.repo, quote(self.revision, safe="")
            ),
            json={
                "commit_message": "Transfer " + operation["path"] + " from Hugging Face",
                "actions": [operation],
            },
        )
