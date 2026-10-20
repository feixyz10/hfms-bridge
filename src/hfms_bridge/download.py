"""Direct downloads with bounded file concurrency and persistent byte resume."""

import fnmatch
import hashlib
import logging
import os
import signal
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Event, current_thread, main_thread

from filelock import FileLock

from .download_state import (
    DownloadState,
    Snapshot,
    read_json,
    target_path,
    validate_paths,
    write_json,
)
from .http import RetryableTransferError, TransferError, error_stage, retry_call
from .hubs import File, HuggingFace, ModelScope
from .progress import Progress

log = logging.getLogger("hfms-bridge")


@dataclass
class DownloadOptions:
    hub: str
    repo_id: str
    local_dir: object = None
    preset: bool = False
    layout: str = None
    repo_type: str = "auto"
    revision: str = None
    endpoint: str = None
    filenames: tuple = ()
    include: tuple = ()
    exclude: tuple = ()
    max_workers: int = 4
    attempts: int = 3
    timeout: int = 120
    chunk_size: int = 1024 * 1024
    progress: str = "auto"
    force_download: bool = False
    dry_run: bool = False
    quiet: bool = False

    def __post_init__(self):
        if self.hub not in ("hf", "ms") or self.repo_type not in ("auto", "model", "dataset"):
            raise ValueError("Invalid hub or repository type")
        if (self.local_dir is None) == (not self.preset):
            raise ValueError("Specify exactly one of --local-dir or --preset")
        if self.preset and self.layout is not None:
            raise ValueError("--preset cannot be combined with --layout")
        if self.layout not in (None, "direct", "repo"):
            raise ValueError("Invalid layout")
        if self.progress not in ("auto", "on", "off"):
            raise ValueError("Invalid progress mode")
        if any(
            isinstance(n, bool) or not isinstance(n, int) or n <= 0
            for n in (
                self.max_workers,
                self.attempts,
                self.timeout,
                self.chunk_size,
            )
        ):
            raise ValueError("workers, attempts, timeout and chunk-size must be positive integers")
        if self.local_dir is not None and not str(self.local_dir).strip():
            raise ValueError("--local-dir cannot be empty")
        if self.revision is None:
            self.revision = "main" if self.hub == "hf" else "master"
        if not self.revision:
            raise ValueError("Revision cannot be empty")
        if self.endpoint is None:
            self.endpoint = (
                os.getenv("HF_ENDPOINT", "https://huggingface.co")
                if self.hub == "hf"
                else os.getenv("MODELSCOPE_ENDPOINT", "https://modelscope.cn")
            )


class Download:
    def __init__(self, options, token=None):
        self.options, self.token = options, token
        self.cancel = Event()
        self.interrupted = False
        self.progress = Progress("off" if options.quiet or options.dry_run else options.progress)

    def _source(self, kind=None):
        opt = self.options
        if opt.hub == "hf":
            return HuggingFace(
                opt.repo_id, kind or opt.repo_type, self.token, opt.endpoint, opt.timeout
            )
        return ModelScope(
            opt.repo_id,
            kind or opt.repo_type,
            self.token,
            revision=opt.revision,
            endpoint=opt.endpoint,
            timeout=opt.timeout,
        )

    def _check(self):
        if self.cancel.is_set():
            raise TransferError("Download interrupted")

    def _retry(self, operation):
        def backoff(delay):
            self.cancel.wait(delay)
            self._check()

        def report(reason, attempt, delay):
            log.warning(
                "%s%s; retrying %s/%s in %ss",
                f"{self.progress.path!r}: " if self.progress.path else "",
                reason,
                attempt,
                self.options.attempts,
                delay,
            )

        return retry_call(operation, self.options.attempts, self._check, backoff, report)

    @contextmanager
    def _interrupts(self):
        previous = None
        if current_thread() is main_thread():
            previous = signal.getsignal(signal.SIGINT)

            def interrupt(signum, frame):
                self.interrupted = True
                self.cancel.set()

            signal.signal(signal.SIGINT, interrupt)
        try:
            yield
        finally:
            if previous is not None:
                signal.signal(signal.SIGINT, previous)
            if self.interrupted:
                raise KeyboardInterrupt

    def _root(self, kind):
        opt = self.options
        if opt.preset:
            variable = "HF_MODELS" if kind == "model" else "HF_DATASETS"
            value = os.getenv(variable)
            if not value or not value.strip() or not Path(value).expanduser().is_dir():
                raise TransferError(
                    f"--preset requires {variable} to point to an existing directory"
                )
            base = Path(value).expanduser().resolve()
        else:
            base = Path(opt.local_dir).expanduser().absolute()
        if opt.preset or opt.layout == "repo":
            validate_paths([File(opt.repo_id, 0)])
            root = target_path(base, opt.repo_id)
        else:
            root = base
        # Explicit ancestor symlinks may be user-chosen, but never accept a symlink
        # for the repository root or its downloaded descendants.
        if root.is_symlink():
            raise TransferError("Download root must not be a symlink")
        root = root.resolve()
        if not root.name or (root.exists() and not root.is_dir()):
            raise TransferError("Download destination must be a non-root directory")
        return root

    def _selected(self, files):
        opt = self.options
        names = {f.path for f in files}
        missing = set(opt.filenames) - names
        if missing:
            raise TransferError("Requested files do not exist: " + ", ".join(sorted(missing)))
        return [
            f
            for f in files
            if (not opt.filenames or f.path in opt.filenames)
            and (not opt.include or any(fnmatch.fnmatchcase(f.path, p) for p in opt.include))
            and not any(fnmatch.fnmatchcase(f.path, p) for p in opt.exclude)
        ]

    def _manifest(self, source, state, identity):
        old = read_json(state.manifest_path)
        if old is not None and (old.get("schema") != 1 or old.get("identity") != identity):
            raise TransferError(
                "Download directory belongs to another repository/revision; use another directory"
            )
        if old is not None:
            try:
                if not isinstance(old["commit"], str) or not isinstance(old["files"], list):
                    raise ValueError()
                for entry in old["files"]:
                    File(**entry)
            except (KeyError, TypeError, ValueError) as exc:
                raise TransferError("Invalid download manifest") from exc
        if old and not old.get("complete") and not self.options.force_download:
            return old
        snapshot = Snapshot()
        source.state = snapshot
        commit = self._retry(lambda: source.resolve(self.options.revision))
        files = (
            snapshot.files
            if self.options.hub == "ms"
            else self._retry(lambda: list(source.files(commit)))
        )
        return {
            "schema": 1,
            "identity": identity,
            "commit": commit,
            "files": [asdict(f) for f in files],
            "complete": False,
        }

    def run(self):
        source = self._source()
        try:
            with self._interrupts():
                kind = self._retry(source.detect)
                root = self._root(kind)
                state = DownloadState(root)
                identity = {
                    "hub": self.options.hub,
                    "repo": source.repo,
                    "type": kind,
                    "revision": self.options.revision,
                    "endpoint": source.endpoint,
                }
                if self.options.dry_run:
                    manifest = self._manifest(source, state, identity)
                    files = self._selected([File(**f) for f in manifest["files"]])
                    validate_paths(files)
                    for file in files:
                        target_path(root, file.path)
                    return {
                        "dry_run": True,
                        "local_dir": str(root),
                        "files": len(files),
                        "bytes": sum(f.size for f in files),
                        "source_commit": manifest["commit"],
                    }
                state.prepare()
                with FileLock(str(state.directory / "task.lock"), timeout=0):
                    manifest = self._manifest(source, state, identity)
                    files = self._selected([File(**f) for f in manifest["files"]])
                    validate_paths(files)
                    for file in files:
                        target_path(root, file.path)
                    manifest["complete"] = False
                    write_json(state.manifest_path, manifest)
                    if not self.options.quiet:
                        log.info(
                            "Download: hub=%s repo=%s repo-type=%s revision=%s",
                            self.options.hub,
                            source.repo,
                            kind,
                            self.options.revision,
                        )
                        log.info("Source snapshot: %s", manifest["commit"])
                        log.info("Local directory: %s", root)
                        log.info(
                            "Selected: %s files, %s bytes; max-workers=%s",
                            len(files),
                            sum(file.size for file in files),
                            self.options.max_workers,
                        )
                    self.progress.start(len(files))
                    result = self._run_files(files, manifest["commit"], state, kind)
                    self._check()
                    manifest["complete"] = True
                    write_json(state.manifest_path, manifest)
                    return dict(result, local_dir=str(root))
        finally:
            source.close()
            self.progress.close()

    def _run_files(self, files, commit, state, kind):
        counts = {"downloaded": 0, "skipped": 0}
        iterator = iter(files)
        failure = None
        with ThreadPoolExecutor(
            max_workers=self.options.max_workers, thread_name_prefix="hfms-download"
        ) as pool:
            pending = set()
            try:
                while pending or not failure:
                    self._check()
                    while not failure and len(pending) < self.options.max_workers:
                        file = next(iterator, None)
                        if file is None:
                            break
                        pending.add(pool.submit(self._file, file, commit, state, kind))
                    if not pending:
                        break
                    done, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
                    for future in done:
                        try:
                            counts[future.result()] += 1
                        except Exception as exc:
                            failure = failure or exc
                if failure:
                    raise failure
            except BaseException:
                self.cancel.set()
                for future in pending:
                    future.cancel()
                raise  # Executor joins before state lock and progress are released.
        return counts

    def _hash(self, path, size):
        sha, git = hashlib.sha256(), hashlib.sha1(f"blob {size}\0".encode(), usedforsecurity=False)
        with path.open("rb") as inp:
            while True:
                self._check()
                chunk = inp.read(self.options.chunk_size)
                if not chunk:
                    break
                sha.update(chunk)
                git.update(chunk)
        return sha, git

    def _file(self, file, commit, state, kind):
        source = self._source(kind)
        try:
            with self.progress.file(file.path, file.size):
                if self.options.force_download:
                    state.paths(file.path)[1].unlink(missing_ok=True)
                result = self._retry(lambda: self._download(source, file, commit, state))
                self.progress.advance()
                return result
        finally:
            source.close()

    def _download(self, source, file, commit, state):
        self._check()
        target = target_path(state.root, file.path)
        meta_path, partial = state.paths(file.path)
        meta = read_json(meta_path) or {}
        fingerprint = dict(asdict(file), source_commit=file.revision or commit)
        completed = meta.get("completed") or {}
        if not isinstance(completed, dict):
            raise TransferError("Invalid completed-file metadata")
        if target.exists():
            if not target.is_file():
                raise TransferError(f"Destination is not a regular file: {file.path!r}")
            self.progress.phase("Checking", reset=True)
            sha, git = self._hash(target, target.stat().st_size)
            same_size = target.stat().st_size == file.size
            same = same_size and (
                (file.sha256 and sha.hexdigest() == file.sha256)
                or (file.git_sha1 and git.hexdigest() == file.git_sha1)
                or (
                    completed.get("file") == fingerprint
                    and completed.get("sha256") == sha.hexdigest()
                )
            )
            if same and not self.options.force_download:
                write_json(
                    meta_path, {"completed": {"file": fingerprint, "sha256": sha.hexdigest()}}
                )
                partial.unlink(missing_ok=True)
                self.progress.info("Skip identical: %s", file.path)
                return "skipped"
            managed = completed.get("sha256") == sha.hexdigest()
            if not self.options.force_download and not managed:
                raise TransferError(
                    f"Local file differs or cannot be verified: {file.path!r}; use --force-download"
                )
        if meta.get("pending") != fingerprint:
            partial.unlink(missing_ok=True)
        meta["pending"] = fingerprint
        write_json(meta_path, meta)
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > file.size or (self.options.hub == "ms" and not file.lfs and offset < file.size):
            partial.unlink(missing_ok=True)
            offset = 0
        self.progress.phase("Downloading", reset=True)
        self.progress.update(offset)
        sha, git = (
            self._hash(partial, file.size)
            if offset
            else (
                hashlib.sha256(),
                hashlib.sha1(f"blob {file.size}\0".encode(), usedforsecurity=False),
            )
        )
        if offset < file.size:
            try:
                response = source.open(file, commit, offset=offset)
            except RetryableTransferError:
                if not offset:
                    raise
                # Invalid Range must never be appended to existing bytes.
                partial.unlink(missing_ok=True)
                log.warning("%r: Range unavailable; restarting file", file.path)
                raise
            with response, error_stage(f"{source.label} download"):
                with partial.open("ab" if offset else "wb") as out:
                    while offset < file.size:
                        self._check()
                        data = response.raw.read(min(self.options.chunk_size, file.size - offset))
                        if not data:
                            raise RetryableTransferError("Source ended early")
                        out.write(data)
                        sha.update(data)
                        git.update(data)
                        offset += len(data)
                        self.progress.update(len(data))
                    if response.raw.read(1):
                        raise TransferError("Source exceeded its declared size")
                    out.flush()
                    os.fsync(out.fileno())
        elif not partial.exists():
            partial.touch()  # Empty file.
        if (file.sha256 and sha.hexdigest() != file.sha256) or (
            file.git_sha1 and git.hexdigest() != file.git_sha1
        ):
            partial.unlink(missing_ok=True)
            raise RetryableTransferError("Source digest mismatch; restarting file")
        self._check()
        target = target_path(state.root, file.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(partial, target)
        write_json(meta_path, {"completed": {"file": fingerprint, "sha256": sha.hexdigest()}})
        if not file.sha256 and not file.git_sha1:
            log.warning("%r: source has no trusted digest; verified length only", file.path)
        self.progress.info("Downloaded: %s", file.path)
        return "downloaded"
