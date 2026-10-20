"""Local download metadata and safe paths, separate from repository contents."""

import hashlib
import json
import os
import re
import unicodedata
from dataclasses import asdict
from pathlib import PureWindowsPath

from .http import TransferError


def read_json(path):
    if path.is_symlink():
        raise TransferError(f"Refusing symlink: {path}")
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise TransferError(f"Invalid download metadata: {path}") from exc
    if not isinstance(value, dict):
        raise TransferError(f"Invalid download metadata: {path}")
    return value


def write_json(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    if path.is_symlink() or tmp.is_symlink():
        raise TransferError("Refusing symlink in download metadata")
    with tmp.open("w", encoding="utf-8") as out:
        json.dump(value, out, ensure_ascii=True, sort_keys=True)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)


def validate_paths(files):
    """Reject cross-platform path escapes and aliases before writing any payload."""
    names = set()
    for file in files:
        for part in file.path.split("/"):
            windows = PureWindowsPath(part)
            if (
                part in ("", ".", "..")
                or windows.drive
                or windows.root
                or re.search(r'[<>:"\\|?*\x00-\x1f]', part)
                or part.endswith((".", " "))
                or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part)
            ):
                raise TransferError(f"Unsafe or non-portable repository path: {file.path!r}")
        key = unicodedata.normalize("NFC", unicodedata.normalize("NFC", file.path).casefold())
        if key in names:
            raise TransferError(f"Conflicting repository path: {file.path!r}")
        names.add(key)
    for key in names:
        parent = key.rpartition("/")[0]
        while parent:
            if parent in names:
                raise TransferError(f"File/directory path conflict: {key!r}")
            parent = parent.rpartition("/")[0]


def target_path(root, name):
    path = root
    for part in name.split("/"):
        path = path / part
        if path.is_symlink():
            raise TransferError(f"Refusing destination symlink: {path}")
    return path


class Snapshot:
    """Adapt MS's existing version-pinning logic to a JSON download manifest."""

    def snapshot(self, files):
        self.files = list(files)
        encoded = json.dumps([asdict(f) for f in self.files], sort_keys=True).encode()
        return "ms-snapshot:" + hashlib.sha256(encoded).hexdigest()


class DownloadState:
    def __init__(self, root):
        self.root = root
        self.directory = root.parent / ("." + root.name + ".hfms-download")
        if self.directory.is_symlink():
            raise TransferError("Refusing symlink for download state directory")
        self.manifest_path = self.directory / "manifest.json"

    def paths(self, name):
        key = hashlib.sha256(name.encode()).hexdigest()
        meta, partial = self.directory / (key + ".json"), self.directory / (key + ".incomplete")
        if meta.is_symlink() or partial.is_symlink():
            raise TransferError("Refusing symlink in download state")
        return meta, partial

    def prepare(self):
        if (
            self.directory.exists()
            and not self.manifest_path.exists()
            and any(p.name != "task.lock" for p in self.directory.iterdir())
        ):
            raise TransferError("State directory has unrecognized contents: " + str(self.directory))
        if (self.directory / "task.lock").is_symlink():
            raise TransferError("Refusing symlink for download lock")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.stat().st_dev != self.directory.stat().st_dev:
            raise TransferError(
                "Download directory and sibling state must be on the same filesystem"
            )
