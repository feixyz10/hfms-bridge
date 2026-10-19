"""Regressions discovered by the independent parallel-transfer review."""

import io
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from hf2ms import Transfer, TransferOptions
from hf2ms.http import TransferError
from hf2ms.hubs import File
from hf2ms.state import Cache, State


def test_cache_budget_waits_for_current_checkpoint_transaction(tmp_path):
    path = tmp_path / ("a" * 64 + ".sqlite3")
    with State(path) as state:
        for index in range(400):
            state.mark(f"file-{index:05}", 10, "a" * 64, "uploaded")
        cache = Cache(tmp_path, path.stat().st_size)
        cache.enforce_state(state)
        transaction_open = threading.Event()
        release = threading.Event()
        checking = threading.Event()
        finished = threading.Event()
        errors = []

        def writer():
            with state._mutex, state.db:
                state.db.execute("UPDATE files SET status='committed' WHERE path='file-00000'")
                transaction_open.set()
                assert release.wait(5)

        def check():
            checking.set()
            try:
                cache.enforce_state(state)
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()

        writer_thread = threading.Thread(target=writer)
        checker_thread = threading.Thread(target=check)
        writer_thread.start()
        assert transaction_open.wait(5)
        assert path.with_suffix(".sqlite3-journal").stat().st_size > 0
        checker_thread.start()
        try:
            assert checking.wait(5)
            assert not finished.wait(0.05)
        finally:
            release.set()
            writer_thread.join(5)
            checker_thread.join(5)
        assert finished.is_set()
        assert not errors
        assert state.file("file-00000")[2] == "committed"
        cache.enforce_state(state)
        # A real persistent budget excess must still be rejected.
        cache.max_size = path.stat().st_size - 1
        with pytest.raises(TransferError, match="Cache limit"):
            cache.enforce_state(state)


def test_cancel_stops_stream_reads_and_retry_backoff(tmp_path, monkeypatch):
    transfer = Transfer(TransferOptions("a/b", "c/d", cache_dir=tmp_path), "secret")

    class CancelledRead(io.BytesIO):
        def read(self, size):
            data = super().read(size)
            transfer._cancel.set()
            return data

    monkeypatch.setattr(
        transfer.source,
        "open",
        lambda *args, **kwargs: SimpleNamespace(raw=CancelledRead(b"abcd"), close=lambda: None),
    )
    try:
        with pytest.raises(TransferError, match="interrupted"):
            transfer._read(File("x", 4), "commit")
        with pytest.raises(TransferError, match="interrupted"):
            transfer._backoff(300)  # Already-set event returns immediately.
    finally:
        transfer.source.close()
        transfer.target.close()


def test_cancel_does_not_start_another_commit(tmp_path, monkeypatch):
    transfer = Transfer(TransferOptions("a/b", "c/d", cache_dir=tmp_path), "secret")
    commits = []

    def read(*args, **kwargs):
        transfer._cancel.set()
        return "a" * 64, b"content"

    monkeypatch.setattr(transfer, "_read", read)
    monkeypatch.setattr(transfer.target, "commit", commits.append)
    try:
        with State(tmp_path / "checkpoint") as state:
            with pytest.raises(TransferError, match="interrupted"):
                transfer._file(File("small.txt", 7), "commit", state)
            assert state.file("small.txt") is None
        assert not commits
    finally:
        transfer.source.close()
        transfer.target.close()


@pytest.mark.skipif(sys.platform == "win32", reason="os.kill(SIGINT) differs on Windows consoles")
def test_repeated_sigint_keeps_checkpoint_and_clients_alive_until_worker_exits(tmp_path):
    # Separate process: signals must never interrupt the pytest runner itself.
    script = r"""
import os, signal, sys, threading, time
from pathlib import Path
from types import SimpleNamespace
from filelock import Timeout
from hf2ms import Transfer, TransferOptions
from hf2ms.hubs import File
from hf2ms.state import Cache, State

path = Path(sys.argv[1]) / 'checkpoint'
started, release = threading.Event(), threading.Event()
errors, closed = [], []
transfer = Transfer(TransferOptions('a/b', 'c/d', workers=2, progress='off'), 'secret')
transfer._selected = lambda _: iter([File('x', 1)])

def process(file, commit, state, exists):
    started.set()
    assert release.wait(5)
    try:
        assert not closed, 'clients closed before worker exit'
        with State(path):
            raise AssertionError('task lock released before worker exit')
    except Timeout:
        pass
    except BaseException as exc:
        errors.append(repr(exc))
    try:
        state.mark('x', 1, 'a' * 64, 'committed')
    except BaseException as exc:
        errors.append(repr(exc))
    return 'committed'

transfer._new_worker = lambda: SimpleNamespace(
    _process_file=process,
    source=SimpleNamespace(close=lambda: closed.append('source')),
    target=SimpleNamespace(close=lambda: closed.append('target')),
)

def send_signals():
    assert started.wait(5)
    os.kill(os.getpid(), signal.SIGINT)
    time.sleep(.15)
    os.kill(os.getpid(), signal.SIGINT)
    time.sleep(.15)
    release.set()

# Force the first signal into the Thread.start()/executor-registration gap.
original_start = threading.Thread.start
def start_with_gap(thread):
    original_start(thread)
    if thread.name.startswith('hf2ms'):
        assert started.wait(5)
        time.sleep(.05)
threading.Thread.start = start_with_gap
sender = threading.Thread(target=send_signals)
sender.start()
try:
    with State(path) as state:
        transfer._run_files('commit', state, Cache(path.parent, 1024**2), True)
except KeyboardInterrupt:
    pass
else:
    raise AssertionError('interruption was swallowed')
finally:
    sender.join(5)
    transfer.source.close()
    transfer.target.close()
# Wait for the fake worker in the buggy implementation too, so late failures
# cannot pass unnoticed merely because the coordinator returned early.
time.sleep(.1)
assert not errors, errors
assert sorted(closed) == ['source', 'target'], closed
assert signal.getsignal(signal.SIGINT) == signal.default_int_handler
with State(path) as state:
    assert state.file('x')[2] == 'committed'
print('safe interruption')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "safe interruption" in result.stdout
