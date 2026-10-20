"""Shared file metadata and streamed LFS transport."""

import re
from dataclasses import dataclass
from ..http import check, payload, error_stage

INLINE_LIMIT = 1024 * 1024
METADATA = {"README.MD", ".GITATTRIBUTES", "CONFIGURATION.JSON"}


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
    revision: str = ""
    lfs: bool = True

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


class BlobTarget:
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
            with error_stage(f"{self.label} verify"):
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
