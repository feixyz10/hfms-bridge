"""Hugging Face REST and LFS adapter."""

import base64
import json
import re
from urllib.parse import quote, urlencode
from .common import File, BlobTarget, repo_name
from ..http import (
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


class HuggingFace(BlobTarget):
    label = "HF"
    default_revision = "main"
    needs_sample = True
    streaming_regular = True

    def __init__(self, repo, repo_type, token=None, endpoint="https://huggingface.co", timeout=120):
        self.repo = repo_name(repo)
        self.repo_type = repo_type
        self.token = token
        self.endpoint = validate_url(endpoint)
        self.http = HTTP(timeout)
        self.info = {}
        self.blob_http = HTTP(timeout)
        self.revision = "main"

    def close(self):
        self.http.close()
        self.blob_http.close()

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
                    yield File(
                        item["path"],
                        item["size"],
                        lfs.get("oid", ""),
                        git_sha1=item.get("oid", "") if not lfs else "",
                    )
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

    def _api(self, method, route, **kwargs):
        headers = {"Authorization": "Bearer " + self.token} if self.token else {}
        headers.update(kwargs.pop("headers", {}))
        with self.http.api_request(method, self.endpoint + route, headers=headers, **kwargs) as r:
            return payload(r)

    @property
    def route(self):
        return f"/api/{self.repo_type}s/{self.repo}"

    def repo_exists(self):
        try:
            self._api("GET", self.route)
            return True
        except HTTPError as exc:
            if exc.status == 404:
                return False
            raise

    def create_repo(self, visibility, license_name):
        owner, name = self.repo.split("/")
        try:
            self._api(
                "POST",
                "/api/repos/create",
                json={
                    "name": name,
                    "organization": owner,
                    "type": self.repo_type,
                    "private": visibility == "private",
                },
            )
        except HTTPError as exc:
            if exc.status != 409 or not self.repo_exists():
                raise

    def check_revision(self):
        refs = self._api("GET", self.route + "/refs")
        branches = refs.get("branches") if isinstance(refs, dict) else None
        if not isinstance(branches, list):
            raise TransferError("Invalid HF branches response")
        if not branches and self.revision == "main":
            return
        if self.revision not in [b.get("name") for b in branches]:
            raise TransferError("HF destination branch must already exist: " + self.revision)

    def file(self, path):
        entries = self._api(
            "POST",
            self.route + "/paths-info/" + quote(self.revision, safe=""),
            data=urlencode({"paths": [path], "expand": False}, doseq=True).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if not isinstance(entries, list) or len(entries) > 1:
            raise TransferError("Invalid HF path metadata")
        if not entries:
            return None
        entry = entries[0]
        if not isinstance(entry, dict):
            raise TransferError("Invalid HF path metadata")
        if entry.get("path") != path:
            raise TransferError("HF path metadata mismatch")
        if entry.get("type") == "directory":
            return {"directory": True}
        lfs = entry.get("lfs") or {}
        sha = lfs.get("oid", "")
        if sha and not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise TransferError("Invalid HF LFS digest")
        git = entry.get("oid", "") if not lfs else ""
        if git and not re.fullmatch(r"[0-9a-f]{40}", git):
            git = ""
        return {"size": entry.get("size"), "sha256": sha, "git_sha1": git}

    def mode(self, file, sample):
        data = self._api(
            "POST",
            self.route + "/preupload/" + quote(self.revision, safe=""),
            json={
                "files": [
                    {
                        "path": file.path,
                        "size": file.size,
                        "sample": base64.b64encode(sample).decode("ascii"),
                    }
                ],
                "gitIgnore": "",
            },
        )
        entries = data.get("files") if isinstance(data, dict) else None
        if (
            not isinstance(entries, list)
            or len(entries) != 1
            or not isinstance(entries[0], dict)
            or entries[0].get("path") != file.path
        ):
            raise RetryableTransferError("Invalid HF preupload response")
        entry = entries[0]
        if entry.get("shouldIgnore"):
            raise TransferError("HF refused a selected file due to ignore rules")
        mode = entry.get("uploadMode")
        if mode not in ("regular", "lfs"):
            raise TransferError("Unsupported HF upload mode")
        return "normal" if mode == "regular" or file.size == 0 else "lfs"

    def negotiate(self, sha256, size):
        prefix = "datasets/" if self.repo_type == "dataset" else ""
        result = self._api(
            "POST",
            f"/{prefix}{self.repo}.git/info/lfs/objects/batch",
            headers={
                "Accept": "application/vnd.git-lfs+json",
                "Content-Type": "application/vnd.git-lfs+json",
            },
            json={
                "operation": "upload",
                "transfers": ["basic", "multipart"],
                "hash_algo": "sha256",
                "ref": {"name": self.revision},
                "objects": [{"oid": sha256, "size": size}],
            },
        )
        objects = result.get("objects") if isinstance(result, dict) else None
        if (
            not isinstance(objects, list)
            or len(objects) != 1
            or not isinstance(objects[0], dict)
            or objects[0].get("oid") != sha256
            or objects[0].get("size") != size
        ):
            raise RetryableTransferError("HF LFS batch omitted or mismatched the requested blob")
        if objects[0].get("error"):
            raise TransferError("HF rejected the LFS blob")
        if result.get("transfer", "basic") not in ("basic", "multipart"):
            raise TransferError("Unsupported HF LFS transfer")
        actions = objects[0].get("actions")
        if actions is None or actions == {}:
            return None
        if not isinstance(actions, dict):
            raise RetryableTransferError("Invalid HF LFS actions")
        for key in ("upload", "verify"):
            if key == "upload" or key in actions:
                action = actions.get(key)
                if not isinstance(action, dict) or not isinstance(action.get("href"), str):
                    raise RetryableTransferError("Invalid HF LFS action")
                validate_url(action["href"])
        return actions

    def _action_headers(self, action):
        url = action["href"]
        validate_url(url)
        headers = dict(action.get("header") or {})
        # Signed storage URLs receive only their explicit action headers.
        if origin(url) == origin(self.endpoint):
            headers.setdefault("Authorization", "Bearer " + self.token)
        return headers

    def upload(self, actions, stream, sha256, size):
        action = actions["upload"]
        headers = action.get("header") or {}
        if "chunk_size" not in headers:
            return super().upload(actions, stream, sha256, size)
        try:
            chunk = int(headers["chunk_size"])
            count = (size + chunk - 1) // chunk
            # HF may zero-pad keys ("00001"). Keep each original URL paired
            # with its numeric index rather than looking it up again as "1".
            numbered = sorted(
                ((int(k), url) for k, url in headers.items() if k.isdigit()),
                key=lambda part: part[0],
            )
            numbers = [number for number, _ in numbered]
            if chunk <= 0 or numbers != list(range(1, count + 1)):
                raise ValueError()
            urls = [url for _, url in numbered]
            for url in urls:
                validate_url(url)
        except (ValueError, TypeError, ZeroDivisionError, KeyError) as exc:
            raise RetryableTransferError("Invalid HF multipart instructions") from exc
        parts = []
        for number, url in enumerate(urls, 1):
            length = min(chunk, size - (number - 1) * chunk)
            part = PartStream(stream, length)
            with self.blob_http.request(
                "PUT",
                url,
                data=part,
                headers={"Content-Length": str(length)},
                allow_redirects=False,
            ) as r:
                check(r)
                if part.remaining or not r.headers.get("ETag"):
                    raise RetryableTransferError("HF part was incomplete or omitted ETag")
                parts.append({"partNumber": number, "etag": r.headers["ETag"]})
        stream.verify()
        with self.blob_http.request(
            "POST",
            action["href"],
            json={"oid": sha256, "parts": parts},
            headers={"Content-Type": "application/vnd.git-lfs+json"},
            allow_redirects=False,
        ) as r:
            check(r)
        if "verify" in actions:
            verify = actions["verify"]
            with error_stage("HF verify"):
                with self.blob_http.request(
                    "POST",
                    verify["href"],
                    headers=self._action_headers(verify),
                    json={"oid": sha256, "size": size},
                    allow_redirects=False,
                ) as r:
                    check(r)

    def commit(self, operation):
        header = {
            "key": "header",
            "value": {
                "summary": "Transfer " + operation["path"] + " from ModelScope",
                "description": "",
            },
        }
        if operation["type"] == "lfs":
            value = {
                "key": "lfsFile",
                "value": {
                    "path": operation["path"],
                    "algo": "sha256",
                    "oid": operation["sha256"],
                    "size": operation["size"],
                },
            }
        else:
            value = {
                "key": "file",
                "value": {
                    "path": operation["path"],
                    "encoding": "base64",
                    "content": operation["content"],
                },
            }
        body = (json.dumps(header) + "\n" + json.dumps(value) + "\n").encode()
        return self._commit_body(body)

    def _commit_body(self, body):
        # Never replay a consumed commit stream on redirect.
        with self.http.request(
            "POST",
            self.endpoint + self.route + "/commit/" + quote(self.revision, safe=""),
            data=body,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/x-ndjson",
                "Content-Length": str(len(body)),
            },
            allow_redirects=False,
        ) as r:
            result = payload(r)
            if not isinstance(result, dict) or not result.get("commitOid"):
                raise RetryableTransferError("HF commit response omitted commitOid")
            return result

    def commit_stream(self, file, stream):
        return self._commit_body(RegularCommit(file, stream))


class PartStream:
    """A bounded view of a sequential stream, with no seeking or buffering."""

    def __init__(self, stream, size):
        self.stream, self.size, self.remaining = stream, size, size

    def __len__(self):
        return self.size

    def read(self, size=-1):
        if not self.remaining:
            return b""
        chunk = self.stream.read(min(self.remaining, size) if size >= 0 else self.remaining)
        self.remaining -= len(chunk)
        return chunk


class RegularCommit:
    """Stream an NDJSON base64 file with known Content-Length and bounded memory."""

    def __init__(self, file, stream):
        self.stream = stream
        header = {"key": "header", "value": {"summary": "Transfer " + file.path, "description": ""}}
        self.prefix = (
            json.dumps(header)
            + '\n{"key":"file","value":{"path":'
            + json.dumps(file.path)
            + ',"encoding":"base64","content":"'
        ).encode()
        self.suffix = b'"}}\n'
        self.size = len(self.prefix) + ((file.size + 2) // 3) * 4 + len(self.suffix)
        self.pending = b""
        self.tail = b""
        self.done = False

    def __len__(self):
        return self.size

    def read(self, size=-1):
        limit = min(size, 65536) if size >= 0 else 65536
        if not limit:
            return b""
        if self.prefix:
            out, self.prefix = self.prefix[:limit], self.prefix[limit:]
            return out
        while not self.pending and not self.done:
            chunk = self.stream.read(49152)
            if chunk:
                chunk = self.tail + chunk
                end = len(chunk) // 3 * 3
                self.pending = base64.b64encode(chunk[:end])
                self.tail = chunk[end:]
            else:
                self.stream.verify()
                self.pending = base64.b64encode(self.tail) + self.suffix
                self.tail = b""
                self.done = True
        out, self.pending = self.pending[:limit], self.pending[limit:]
        return out
