"""A file counter and reusable per-thread byte rows; no payload storage."""

import logging
import sys
from contextlib import contextmanager
from threading import RLock, local

from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm


class Progress:
    def __init__(self, mode="auto"):
        self.enabled = mode == "on" or (mode == "auto" and sys.stderr.isatty())
        self.total = None
        self._local = local()
        self._rows = {}
        self._mutex = RLock()
        self._logs = None

    @property
    def current(self):
        return getattr(self._local, "bar", None)

    @property
    def path(self):
        return getattr(self._local, "path", "")

    def start(self, count):
        if not self.enabled:
            return
        self._logs = logging_redirect_tqdm()
        self._logs.__enter__()
        self.total = tqdm(
            total=count,
            desc="Files",
            unit="file",
            position=0,
            dynamic_ncols=True,
            file=sys.stderr,
            leave=True,
        )

    @contextmanager
    def file(self, path, size):
        # Escape control characters in repository-controlled filenames.
        self._local.path = "".join(c if c.isprintable() else repr(c)[1:-1] for c in path)
        position = None
        if self.enabled:
            with self._mutex:
                position = 1
                while position in self._rows:
                    position += 1
                self._local.bar = tqdm(
                    total=size,
                    desc=self.path,
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1024,
                    position=position,
                    dynamic_ncols=True,
                    file=sys.stderr,
                    leave=False,
                )
                self._rows[position] = self.current
        try:
            yield
        finally:
            if self.current is not None:
                with self._mutex:
                    self.current.close()
                    del self._rows[position]
                    self._local.bar = None
            self._local.path = ""

    def phase(self, label, reset=False):
        if self.current is not None:
            if reset:
                self.current.reset()
            self.current.set_description_str(f"{self.path} [{label}]")

    def update(self, size):
        if self.current is not None:
            self.current.update(size)

    def advance(self):
        with self._mutex:
            if self.total is not None:
                self.total.update(1)

    def info(self, message, *args):
        if not self.enabled:
            logging.getLogger("hfms-bridge").info(message, *args)

    def close(self):
        # Called only after all workers have finished.
        for bar in self._rows.values():
            bar.close()
        self._rows.clear()
        if self.total is not None:
            self.total.close()
            self.total = None
        if self._logs is not None:
            self._logs.__exit__(None, None, None)
            self._logs = None
