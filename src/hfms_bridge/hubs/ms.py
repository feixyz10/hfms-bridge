"""ModelScope REST adapter."""

import logging
import re
from urllib.parse import quote, urlencode, urlparse
from .common import File, BlobTarget, INLINE_LIMIT, METADATA, repo_name
from ..http import (
    HTTP,
    HTTPError,
    RetryableTransferError,
    TransferError,
    origin,
    payload,
    validate_url,
)


class ModelScope(BlobTarget):
    label = "MS"
    default_revision = "master"
    needs_sample = False
    streaming_regular = False

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
        self.info = {}
        self.state = None
        self.http = HTTP(timeout)
        self.blob_http = HTTP(timeout)
        self._cached_root = None
        self._cached_entries = {}

    def close(self):
        self.http.close()
        self.blob_http.close()

    def _api(self, method, path, **kwargs):
        headers = (
            {"Authorization": "Bearer " + self.token, "Cookie": "m_session_id=" + self.token}
            if self.token
            else {}
        )
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
            logging.getLogger("hfms-bridge").warning(
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

    def mode(self, file, sample=b""):
        if file.path.rsplit("/", 1)[-1].upper() in METADATA:
            if file.size > INLINE_LIMIT:
                raise TransferError("Repository metadata exceeds the 1 MiB inline limit")
            return "normal"
        return "normal" if file.size <= INLINE_LIMIT and not file.sha256 else "lfs"

    def detect(self):
        found, denied = [], []
        for kind in ("model", "dataset") if self.repo_type == "auto" else (self.repo_type,):
            try:
                info = self._api("GET", f"{kind}s/{self.repo}")
                if not isinstance(info, dict):
                    raise TransferError("Invalid MS repository metadata")
                visibility = info.get("Visibility")
                found.append(
                    (
                        kind,
                        {
                            "private": False if visibility == 5 else True,
                            "cardData": {"license": info.get("License")},
                        },
                    )
                )
            except HTTPError as exc:
                if exc.status in (401, 403):
                    denied.append(exc)
                elif exc.status != 404:
                    raise
        if not found:
            if denied:
                raise denied[0]
            raise TransferError("MS repository not found or not accessible")
        if len(found) != 1:
            raise TransferError("Both model and dataset exist; specify --repo-type")
        self.repo_type, self.info = found[0]
        return self.repo_type

    def _page(self, revision, root="", page=1):
        params = {"Revision": revision, "Root": root or "/", "Recursive": "False"}
        if self.repo_type == "dataset":
            params.update(Recursive="True", PageNumber=page, PageSize=200)
        suffix = "tree" if self.repo_type == "dataset" else "files"
        result = self._api("GET", f"{self.repo_type}s/{self.repo}/repo/{suffix}", params=params)
        entries = result.get("Files") if isinstance(result, dict) else None
        if not isinstance(entries, list):
            raise TransferError("Invalid MS source listing")
        if self.repo_type == "model" and len(entries) >= 3000:
            raise TransferError("MS source directory may be truncated at 3000 entries")
        return result, entries

    def _files(self, revision):
        # Walk model directories; dataset listings are recursive and paginated.
        roots = [""]
        while roots:
            root, page, previous = roots.pop(), 1, None
            while True:
                _, entries = self._page(revision, root, page)
                signature = tuple(entry.get("Path") for entry in entries)
                if entries and signature == previous:
                    raise TransferError("MS source pagination did not advance")
                previous = signature
                for entry in entries:
                    path = entry.get("Path")
                    if entry.get("Type") == "tree":
                        if self.repo_type == "model":
                            # Only immediate children are valid for a directory walk.
                            if not path or path.rpartition("/")[0] != root:
                                raise TransferError("Invalid MS source directory path")
                            File(path, 0)
                            roots.append(path)
                        continue
                    if entry.get("Type") != "blob":
                        raise TransferError("Unsupported MS source entry")
                    if not isinstance(path, str) or (
                        self.repo_type == "model" and path.rpartition("/")[0] != root
                    ):
                        raise TransferError("MS source listing ignored directory scope")
                    commit = entry.get("Revision", "")
                    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
                        raise TransferError("MS file omitted its immutable revision")
                    yield File(
                        path,
                        entry["Size"],
                        entry.get("Sha256") or "",
                        commit,
                        bool(entry.get("IsLFS")),
                    )
                if self.repo_type != "dataset" or len(entries) < 200:
                    break
                page += 1

    def resolve(self, revision):
        if self.state is None or getattr(self, "read_only", False):
            # Read-only preview: files are listed without persisting a snapshot.
            return revision
        before, _ = self._page(revision)
        marker = before.get("LatestCommitter")

        def files():
            yield from self._files(revision)
            after, _ = self._page(revision)
            if after.get("LatestCommitter") != marker:
                raise RetryableTransferError("MS source changed while taking its snapshot")

        return self.state.snapshot(files())

    def files(self, commit):
        if commit.startswith("ms-snapshot:"):
            if self.state is None:
                raise TransferError("Missing MS source snapshot")
            yield from self.state.source_files()
        else:
            yield from self._files(commit)

    def open(self, file, commit, offset=0):
        if offset and not file.lfs:
            raise RetryableTransferError("MS regular files cannot safely resume with Range")
        revision = file.revision or commit
        url = (
            self.endpoint
            + f"/api/v1/{self.repo_type}s/{self.repo}/repo?"
            + urlencode(
                {
                    "Revision": revision,
                    "FilePath": file.path,
                }
            )
        )
        # download() strips auth on cross-origin CDN redirects and validates Range.
        response = self.http.download(url, self.token, offset=offset, total=file.size)
        length = response.headers.get("Content-Length")
        if length is not None and length != str(file.size - offset):
            response.close()
            raise TransferError("MS source length differs from snapshot")
        return response
