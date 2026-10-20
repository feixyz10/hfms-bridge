"""Transfer engine and public Python entry point."""

import base64
import fnmatch
import logging
import signal
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock, current_thread, local, main_thread
from types import SimpleNamespace
from typing import Tuple


from .http import (
    CheckedStream,
    ResumingReader,
    TransferError,
    error_stage,
    retry_call,
)
from .hubs import HuggingFace, ModelScope
from .hubs.common import INLINE_LIMIT
from .state import Cache
from .progress import Progress

log = logging.getLogger("hfms-bridge")


@dataclass
class TransferOptions:
    source: str
    destination: str
    direction: str = "hf2ms"
    repo_type: str = "auto"
    hf_revision: str = "main"
    ms_revision: str = "master"
    cache_dir: Path = field(default_factory=lambda: Path.home() / ".cache" / "hfms-bridge")
    cache_max_size: int = 256 * 1024 * 1024
    visibility: str = "auto"
    on_conflict: str = "overwrite"
    restart: bool = False
    dry_run: bool = False
    hf_endpoint: str = "https://huggingface.co"
    ms_endpoint: str = "https://modelscope.cn"
    chunk_size: int = 1024 * 1024
    attempts: int = 3
    timeout: int = 120
    progress: str = "auto"
    workers: int = 4
    include: Tuple[str, ...] = ()
    exclude: Tuple[str, ...] = ()

    def __post_init__(self):
        if self.direction not in ("hf2ms", "ms2hf"):
            raise ValueError("direction must be hf2ms or ms2hf")
        if isinstance(self.workers, bool) or not isinstance(self.workers, int) or self.workers < 1:
            raise ValueError("workers must be a positive integer")
        if self.progress not in ("auto", "on", "off"):
            raise ValueError("progress must be auto, on or off")
        if self.repo_type not in ("auto", "model", "dataset"):
            raise ValueError("repo_type must be auto, model or dataset")
        if self.visibility not in ("auto", "private", "public"):
            raise ValueError("Invalid visibility")
        if self.on_conflict not in ("overwrite", "error", "skip"):
            raise ValueError("Invalid conflict policy")
        if self.cache_max_size < 65536:
            raise ValueError("cache_max_size must be at least 64 KiB")
        if not self.hf_revision or not self.ms_revision:
            raise ValueError("Revisions cannot be empty")
        if self.chunk_size <= 0 or self.attempts <= 0 or self.timeout <= 0:
            raise ValueError("chunk_size, attempts and timeout must be positive")


class Transfer:
    def __init__(self, options: TransferOptions, ms_token=None, hf_token=None):
        target_token = ms_token if options.direction == "hf2ms" else hf_token
        if not target_token or not target_token.strip():
            raise ValueError("A non-empty destination token is required")
        self.options = options
        self.ms_token, self.hf_token = ms_token, hf_token
        self.progress = Progress(options.progress)
        self._commit_lock = Lock()
        self._cancel = Event()
        self._interrupt = SimpleNamespace(requested=False)
        forward = options.direction == "hf2ms"
        hf = HuggingFace(
            options.source if forward else options.destination,
            options.repo_type,
            hf_token,
            options.hf_endpoint,
            options.timeout,
        )
        # The public mirror redirects writes to HF. Route the destination before
        # sending any body: streamed commits cannot safely replay a 308 response.
        # Keep options unchanged so existing tasks retain their checkpoint identity.
        if not forward and hf.endpoint == "https://hf-mirror.com":
            hf.endpoint = "https://huggingface.co"
        ms = ModelScope(
            options.destination if forward else options.source,
            options.repo_type,
            ms_token,
            options.ms_revision,
            options.ms_endpoint,
            options.timeout,
        )
        hf.revision = options.hf_revision
        self.source, self.target = (hf, ms) if forward else (ms, hf)
        self.source_revision = options.hf_revision if forward else options.ms_revision

    def _check_cancelled(self):
        if self._cancel.is_set() or self._interrupt.requested:
            raise TransferError("Transfer interrupted")

    def _backoff(self, delay):
        deadline = time.monotonic() + delay
        while True:
            self._check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._cancel.wait(min(remaining, 0.1))

    def _on_read(self, size):
        self._check_cancelled()
        self.progress.update(size)

    def _retry(self, operation):
        def report(reason, attempt, delay):
            path = self.progress.path
            self.progress.phase(f"Retrying {attempt}/{self.options.attempts}")
            log.warning(
                "%s%s; retrying attempt %s/%s in %ss",
                f"{path!r}: " if path else "",
                reason,
                attempt,
                self.options.attempts,
                delay,
            )

        return retry_call(
            operation, self.options.attempts, self._check_cancelled, self._backoff, report
        )

    def _identity(self):
        opt = self.options
        identity = {
            key: getattr(opt, key)
            for key in (
                "direction",
                "source",
                "destination",
                "hf_revision",
                "ms_revision",
                "hf_endpoint",
                "ms_endpoint",
                "visibility",
                "on_conflict",
            )
        }
        identity.update(
            repo_type=self.source.repo_type, include=list(opt.include), exclude=list(opt.exclude)
        )
        return identity

    def _selected(self, commit):
        opt = self.options
        for file in self.source.files(commit):
            if opt.include and not any(fnmatch.fnmatchcase(file.path, p) for p in opt.include):
                continue
            if any(fnmatch.fnmatchcase(file.path, p) for p in opt.exclude):
                continue
            if not self.target.needs_sample:
                self.target.mode(file)  # Validate destination-specific metadata limits early.
            yield file

    def _comparison(self, file, remote, commit, state, dry_run=False):
        if remote is None:
            return "create", file.sha256
        if remote.get("directory"):
            return "directory", file.sha256
        record = state.file(file.path) if state else None
        sha = file.sha256 or (record[1] if record and record[0] == file.size else "")
        if remote.get("size") is not None and remote["size"] != file.size:
            return "overwrite", sha
        if not remote.get("sha256"):
            if remote.get("git_sha1") and not dry_run:
                sha, git = self._retry(lambda: self._read(file, commit, sha, git=True))
                state.mark(file.path, file.size, sha, "hashed")
                return ("same" if git == remote["git_sha1"] else "overwrite"), sha
            return "unknown", sha
        if not sha:
            if dry_run:
                return "unknown", ""
            sha, _ = self._retry(lambda: self._read(file, commit))
            state.mark(file.path, file.size, sha, "hashed")
        return ("same" if sha == remote["sha256"] else "overwrite"), sha

    def _visibility(self):
        if self.options.visibility != "auto":
            return self.options.visibility
        # If metadata is incomplete, never infer that the source is public.
        info = self.source.info
        return "public" if info.get("private") is False and not info.get("gated") else "private"

    def run(self):
        opt = self.options
        cache = Cache(opt.cache_dir, opt.cache_max_size)
        try:
            if opt.direction == "ms2hf" and opt.hf_endpoint.rstrip("/") == "https://hf-mirror.com":
                log.info("Using https://huggingface.co for HF destination requests")
            self.target.repo_type = self._retry(self.source.detect)
            identity = self._identity()
            exists = self._retry(self.target.repo_exists)
            if exists:
                self._retry(self.target.check_revision)
            elif self.target.revision != self.target.default_revision:
                raise TransferError(
                    f"New {self.target.label} repositories start on {self.target.default_revision}; "
                    "create the requested branch first"
                )
            context = cache.read(identity) if opt.dry_run else cache.open(identity)
            with context as state:
                self.source.state = state
                self.source.read_only = opt.dry_run
                pinned = state.get("source_commit") if state else None
                complete = state.get("complete") if state else None
                commit = (
                    pinned
                    if pinned and not complete and not opt.restart
                    else self._retry(lambda: self.source.resolve(self.source_revision))
                )
                if state and not opt.dry_run:
                    if commit != pinned or opt.restart:
                        state.reset(commit)
                    state.set("complete", False)
                    cache.enforce_state(state)
                # Completed state for an older source commit must not inform comparisons.
                read_state = state if commit == pinned and not opt.restart else None
                log.info(
                    "%s %s %s @ %s -> %s %s @ %s",
                    self.source.label,
                    self.source.repo_type,
                    opt.source,
                    commit,
                    self.target.label,
                    opt.destination,
                    self.target.revision,
                )
                if opt.dry_run:
                    return self._preview(commit, exists, read_state)
                if opt.on_conflict == "error" and exists:
                    conflicts = 0
                    for file in self._selected(commit):
                        remote = self._retry(lambda file=file: self.target.file(file.path))
                        decision, _ = self._comparison(file, remote, commit, state)
                        if decision not in ("create", "same"):
                            conflicts += 1
                            log.error("Conflict (%s): %s", decision, file.path)
                        cache.enforce_state(state)
                    if conflicts:
                        raise TransferError(f"{conflicts} conflict(s); no remote writes performed")
                    self.target._cached_root = None
                if not exists:
                    card = self.source.info.get("cardData")
                    license_name = card.get("license") if isinstance(card, dict) else None
                    if not isinstance(license_name, str):
                        license_name = "Other"
                    self._retry(lambda: self.target.create_repo(self._visibility(), license_name))
                    log.info(
                        "Created %s repository: %s (%s)",
                        self.target.label,
                        opt.destination,
                        self._visibility(),
                    )
                if self.progress.enabled:
                    self.progress.start(self._retry(lambda: sum(1 for _ in self._selected(commit))))
                counts = self._run_files(commit, state, cache, exists)
                state.set("complete", True)
                cache.enforce_state(state)
                return counts
        finally:
            self.progress.close()
            self.source.close()
            self.target.close()

    def _process_file(self, file, commit, state, exists):
        with self.progress.file(file.path, file.size):
            remote = self._retry(lambda: self.target.file(file.path)) if exists else None
            decision, sha = self._comparison(file, remote, commit, state)
            outcome = "skipped"
            if decision == "same":
                state.mark(file.path, file.size, sha, "committed")
                self.progress.phase("Skipped")
                self.progress.info("Skip identical: %s", file.path)
            elif decision != "create" and self.options.on_conflict == "skip":
                self.progress.phase("Skipped conflict")
                self.progress.info("Skip conflict (%s): %s", decision, file.path)
            elif decision == "directory":
                raise TransferError("Destination is a directory: " + file.path)
            elif decision != "create" and self.options.on_conflict == "error":
                raise TransferError("Destination changed after preflight: " + file.path)
            else:
                self.progress.info("Transfer (%s): %s (%s bytes)", decision, file.path, file.size)
                self._retry(lambda: self._file(file, commit, state))
                outcome = "committed"
        self.progress.advance()
        return outcome

    def _new_worker(self):
        # Session cookies, authentication, connection pools and directory caches
        # belong to a single thread. Only progress and the commit mutex are shared.
        worker = Transfer(self.options, self.ms_token, self.hf_token)
        worker.source.state = getattr(self.source, "state", None)
        worker.source.repo_type = self.source.repo_type
        worker.target.repo_type = self.target.repo_type
        worker.progress = self.progress
        worker._commit_lock = self._commit_lock
        worker._cancel = self._cancel
        worker._interrupt = self._interrupt
        return worker

    @contextmanager
    def _worker_pool(self, stopped):
        pool = ThreadPoolExecutor(
            max_workers=self.options.workers, thread_name_prefix="hfms-bridge"
        )
        previous = None

        def interrupt(_signum, _frame):
            # Never raise or acquire locks inside a signal handler: SIGINT may
            # arrive between Thread.start() and executor thread registration.
            self._interrupt.requested = True

        # Signals run only on the main thread. Preserve application-installed
        # handlers; the CLI uses Python's default SIGINT handler.
        if current_thread() is main_thread():
            handler = signal.getsignal(signal.SIGINT)
            if handler == signal.default_int_handler:
                previous = handler
                signal.signal(signal.SIGINT, interrupt)
        try:
            yield pool
        except KeyboardInterrupt:
            self._cancel.set()
            stopped.set()
            log.warning(
                "Interrupted; stopping active transfers and waiting for network calls to return"
            )
            raise
        finally:
            try:
                pool.shutdown(wait=True, cancel_futures=True)
            finally:
                if previous is not None:
                    signal.signal(signal.SIGINT, previous)
            if self._interrupt.requested:
                raise KeyboardInterrupt

    def _run_files(self, commit, state, cache, exists):
        counts = {"committed": 0, "skipped": 0}
        files = iter(self._selected(commit))
        if self.options.workers == 1:
            for file in files:
                counts[self._process_file(file, commit, state, exists)] += 1
                cache.enforce_state(state)
            return counts

        stopped = Event()
        thread = local()
        clients = []
        clients_lock = Lock()

        def perform(file):
            # A failure can arrive between submit() and starting the task.
            if stopped.is_set():
                return None
            try:
                if not hasattr(thread, "worker"):
                    thread.worker = self._new_worker()
                    with clients_lock:
                        clients.append(thread.worker)
                return thread.worker._process_file(file, commit, state, exists)
            except BaseException:
                stopped.set()
                raise

        failure = None
        try:
            with self._worker_pool(stopped) as pool:
                pending = {}
                exhausted = False
                try:
                    while pending or not exhausted:
                        if self._interrupt.requested:
                            raise KeyboardInterrupt
                        # Keep only N file descriptions/futures in flight; never
                        # enqueue a whole repository or retain payload buffers.
                        while (
                            not exhausted
                            and not stopped.is_set()
                            and len(pending) < self.options.workers
                        ):
                            file = next(files, None)
                            if file is None:
                                exhausted = True
                                break
                            if stopped.is_set():
                                break
                            pending[pool.submit(perform, file)] = file.path
                            if self._interrupt.requested:
                                raise KeyboardInterrupt
                        if not pending:
                            break
                        done, _ = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
                        if self._interrupt.requested:
                            raise KeyboardInterrupt
                        for future in done:
                            path = pending.pop(future)
                            try:
                                outcome = future.result()
                            except Exception as exc:
                                stopped.set()
                                if failure is None:
                                    failure = exc
                                    log.warning(
                                        "File failed (%r); finishing active files before exiting",
                                        path,
                                    )
                                else:
                                    log.error("Another file failed: %r", path)
                            else:
                                if outcome is not None:
                                    counts[outcome] += 1
                                    cache.enforce_state(state)
                        if stopped.is_set():
                            exhausted = True
                except BaseException:
                    stopped.set()
                    for future in pending:
                        future.cancel()
                    # The pool joins active workers before state/session cleanup.
                    raise
        finally:
            for worker in clients:
                worker.source.close()
                worker.target.close()
        if failure is not None:
            raise failure
        return counts

    def _preview(self, commit, exists, state):
        report = {
            "dry_run": True,
            "source_commit": commit,
            "repo_type": self.source.repo_type,
            "destination_revision": self.target.revision,
            "direction": self.options.direction,
            "create_repo": not exists,
            "visibility": self._visibility() if not exists else None,
            "files": 0,
            "bytes": 0,
            "create": 0,
            "same": 0,
            "overwrite": 0,
            "unknown": 0,
            "directory": 0,
            "would_commit": 0,
            "would_skip": 0,
            "would_error": 0,
        }
        if not exists:
            log.info("Would create %s repository (%s)", self.target.label, report["visibility"])
        for file in self._selected(commit):
            remote = self._retry(lambda file=file: self.target.file(file.path)) if exists else None
            decision, _ = self._comparison(file, remote, commit, state, dry_run=True)
            report[decision] += 1
            report["files"] += 1
            report["bytes"] += file.size
            policy = self.options.on_conflict
            if decision == "same" or (decision != "create" and policy == "skip"):
                action = "skip"
            elif decision == "directory" or (decision != "create" and policy == "error"):
                action = "error"
            else:
                action = "commit"
            report["would_" + action] += 1
            log.info("Plan %s -> %s: %s (%s bytes)", decision, action, file.path, file.size)
        report["on_conflict"] = self.options.on_conflict
        return report

    def _source_reader(self, file, commit):
        def report(offset, attempt):
            log.warning(
                "%r: %s read interrupted; resuming at byte %s (reconnect %s/%s)",
                file.path,
                self.source.label,
                offset,
                attempt,
                self.options.attempts - 1,
            )

        return ResumingReader(
            lambda offset: self.source.open(file, commit, offset=offset),
            file.size,
            self.options.attempts,
            self._backoff,
            self._check_cancelled,
            report,
            stage=f"{self.source.label} read",
        )

    def _read(self, file, commit, sha256="", collect=False, git=False):
        self.progress.phase(f"{self.source.label} read" if collect else "Hashing", reset=True)
        with self._source_reader(file, commit) as reader:
            stream = CheckedStream(
                reader,
                file.size,
                sha256,
                self.options.chunk_size,
                on_read=self._on_read,
                stage=f"{self.source.label} read",
            )
            chunks = []
            while True:
                data = stream.read()
                if not data:
                    break
                if collect:
                    chunks.append(data)
            stream.verify()
            return stream.hash.hexdigest(), (
                stream.git_hash.hexdigest() if git else b"".join(chunks) if collect else None
            )

    def _file(self, file, commit, state):
        self.progress.phase("Preparing", reset=True)
        record = state.file(file.path)
        sha256 = file.sha256 or (record[1] if record else "")
        sample = b""
        if self.target.needs_sample and file.size:
            with self._source_reader(file, commit) as reader:
                sample = reader.read(min(file.size, 512))
        with error_stage(f"{self.target.label} preupload"):
            mode = self.target.mode(file, sample)
        if mode == "normal" and file.size > INLINE_LIMIT:
            self._commit_regular_stream(file, commit, state, sha256)
            return
        inline = mode == "normal"
        operation = {"action": "create", "path": file.path, "size": file.size}
        if inline:
            sha256, data = self._read(file, commit, sha256, collect=True)
            operation.update(
                type="normal",
                sha256="",
                encoding="base64",
                content=base64.b64encode(data).decode("ascii"),
            )
        else:
            if not sha256:
                self.progress.info("No source SHA-256; streaming hash pass: %s", file.path)
                sha256, _ = self._read(file, commit)
                state.mark(file.path, file.size, sha256, "hashed")
            with error_stage(f"{self.target.label} negotiate"):
                actions = self.target.negotiate(sha256, file.size)
            if actions is not None:
                self.progress.phase(f"{self.source.label} -> {self.target.label}", reset=True)
                with self._source_reader(file, commit) as reader:
                    stream = CheckedStream(
                        reader,
                        file.size,
                        sha256,
                        self.options.chunk_size,
                        on_read=self._on_read,
                        stage=f"{self.source.label} read",
                    )
                    with error_stage(f"{self.target.label} upload"):
                        self.target.upload(actions, stream, sha256, file.size)
            else:
                self.progress.phase("Reused")
                self.progress.info("Reuse complete blob: %s", file.path)
            state.mark(file.path, file.size, sha256, "uploaded")
            operation.update(type="lfs", sha256=sha256, encoding="", content="")
        self.progress.phase("Waiting to commit")
        # Serialize branch mutations, not the potentially long data transfers.
        # Failed attempts release the lock before backoff/re-negotiation.
        with self._commit_lock:
            self._check_cancelled()
            self.progress.phase("Committing")
            with error_stage(f"{self.target.label} commit"):
                self.target.commit(operation)
            state.mark(file.path, file.size, sha256, "committed")

    def _commit_regular_stream(self, file, commit, state, sha256):
        if not self.target.streaming_regular:
            raise TransferError("Destination cannot stream a large regular file")
        self.progress.phase("Waiting to commit")
        with self._commit_lock:
            self._check_cancelled()
            self.progress.phase(f"{self.source.label} -> {self.target.label} commit", reset=True)
            with self._source_reader(file, commit) as reader:
                stream = CheckedStream(
                    reader,
                    file.size,
                    sha256,
                    self.options.chunk_size,
                    on_read=self._on_read,
                    stage=f"{self.source.label} read",
                )
                with error_stage(f"{self.target.label} commit"):
                    self.target.commit_stream(file, stream)
                stream.verify()
                state.mark(file.path, file.size, stream.hash.hexdigest(), "committed")
