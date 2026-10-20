"""Small, transactional checkpoints; never stores credentials or file contents."""

import json
import hashlib
import sqlite3
import time
import re
from contextlib import closing, contextmanager
from pathlib import Path
from threading import RLock

from filelock import FileLock, Timeout

from .http import TransferError


class State:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(str(self.path) + ".lock", timeout=0)
        self._mutex = RLock()
        self.max_size = None

    def __enter__(self):
        self.lock.acquire()
        try:
            # One connection, serialized short transactions. No network I/O
            # runs under this mutex; the file lock still excludes other jobs.
            self.db = sqlite3.connect(str(self.path), check_same_thread=False)
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);"
                "CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, "
                "size INTEGER NOT NULL, sha256 TEXT NOT NULL, status TEXT NOT NULL);"
            )
            return self
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            self.lock.release()
            raise

    def __exit__(self, *args):
        self.db.close()
        self.lock.release()

    def bind(self, identity):
        existing = self.get("identity")
        if existing is not None and existing != identity:
            raise ValueError(
                "State belongs to different transfer options; use another cache directory"
            )
        self.set("identity", identity)

    def get(self, key):
        with self._mutex:
            row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, key, value):
        with self._mutex, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO metadata VALUES (?, ?)",
                (key, json.dumps(value, sort_keys=True)),
            )

    def file(self, path):
        with self._mutex:
            return self.db.execute(
                "SELECT size, sha256, status FROM files WHERE path=?", (path,)
            ).fetchone()

    def mark(self, path, size, sha256, status):
        with self._mutex, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?)", (path, size, sha256, status)
            )

    def reset(self, commit):
        with self._mutex:
            with self.db:
                self.db.execute("DELETE FROM files")
                self.db.execute(
                    "INSERT OR REPLACE INTO metadata VALUES ('source_commit', ?)",
                    (json.dumps(commit),),
                )
                self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('complete', 'false')")
            self.db.execute("VACUUM")

    def snapshot(self, files):
        """Atomically replace only source metadata, never file contents."""
        from dataclasses import asdict

        digest = hashlib.sha256()
        with self._mutex, self.db:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS source_files "
                "(path TEXT PRIMARY KEY, metadata TEXT NOT NULL)"
            )
            self.db.execute("DELETE FROM source_files")
            for file in files:
                encoded = json.dumps(asdict(file), sort_keys=True)
                digest.update(encoded.encode())
                self.db.execute("INSERT INTO source_files VALUES (?, ?)", (file.path, encoded))
                if self.max_size is not None:
                    pages = self.db.execute("PRAGMA page_count").fetchone()[0]
                    page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
                    if pages * page_size > self.max_size:
                        raise TransferError(
                            "MS source snapshot exceeds cache budget; increase --cache-max-size"
                        )
            snapshot_id = "ms-snapshot:" + digest.hexdigest()
            self.db.execute("DELETE FROM files")
            self.db.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('source_commit', ?)",
                (json.dumps(snapshot_id),),
            )
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('complete', 'false')")
        return snapshot_id

    def source_files(self):
        from .hubs import File

        # Keyset iteration keeps memory bounded and releases the mutex between rows.
        last = ""
        while True:
            with self._mutex:
                row = self.db.execute(
                    "SELECT path, metadata FROM source_files WHERE path > ? ORDER BY path LIMIT 1",
                    (last,),
                ).fetchone()
            if row is None:
                return
            last = row[0]
            yield File(**json.loads(row[1]))


class Cache:
    """Evict only completed, unlocked jobs; never discard active recovery data."""

    def __init__(self, directory, max_size):
        self.directory = Path(directory).expanduser().resolve()
        self.max_size = max_size

    def path(self, identity):
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        return self.directory / (key + ".sqlite3")

    def _guard(self):
        return FileLock(str(self.directory / ".cache.lock"), timeout=10)

    @contextmanager
    def read(self, identity):
        """Read existing state without creating dirs, lock files or checkpoints."""
        path = self.path(identity)
        if not path.exists():
            yield None
            return
        state = object.__new__(State)
        state._mutex = RLock()
        try:
            state.db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        except sqlite3.OperationalError:
            # A completed job may have been evicted between exists() and open().
            if not path.exists():
                yield None
                return
            raise
        try:
            yield state
        finally:
            state.db.close()

    @contextmanager
    def open(self, identity):
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path(identity)
        with self._guard():
            self._prune(protected=path)
            state = State(path)
            state.max_size = self.max_size
            state.__enter__()
        try:
            state.bind(identity)
            state.set("last_used", time.time())
            self.enforce(path)
            yield state
        finally:
            state.__exit__(None, None, None)

    def enforce(self, protected):
        with self._guard():
            self._prune(protected)

    def enforce_state(self, state):
        # Inspect the current job between transactions, not while a worker's
        # temporary rollback journal inflates its apparent disk usage.
        with state._mutex:
            self.enforce(state.path)

    def _prune(self, protected):
        files = [
            p
            for p in self.directory.glob("*.sqlite3")
            if not p.is_symlink() and re.fullmatch(r"[0-9a-f]{64}\.sqlite3", p.name)
        ]

        def size():
            total = 0
            for p in self.directory.glob("*.sqlite3*"):
                if not re.fullmatch(r"[0-9a-f]{64}\.sqlite3(?:-journal|-wal|-shm|\.lock)?", p.name):
                    continue
                try:
                    if p.is_file() and not p.is_symlink():
                        total += p.stat().st_size
                except FileNotFoundError:
                    pass  # Another active transaction removed its short-lived journal.
            return total

        if size() <= self.max_size:
            return
        for path in sorted(files, key=lambda p: p.stat().st_mtime):
            if path == protected:
                continue
            lock = FileLock(str(path) + ".lock", timeout=0)
            try:
                with lock:
                    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                        row = db.execute(
                            "SELECT value FROM metadata WHERE key='complete'"
                        ).fetchone()
                    if not row or json.loads(row[0]) is not True:
                        continue
                    path.unlink()
                    # All opens go through the global guard, so removing the idle
                    # lock file afterwards cannot race a managed task starting.
                Path(str(path) + ".lock").unlink(missing_ok=True)
            except (Timeout, sqlite3.Error, ValueError, OSError):
                continue
            if size() <= self.max_size:
                return
        raise TransferError(
            "Cache limit reached; unfinished checkpoints were preserved. "
            "Increase --cache-max-size or remove an unwanted task's state."
        )
