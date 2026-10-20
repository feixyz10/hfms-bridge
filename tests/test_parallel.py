"""Exercise real overlapping HTTP streams and recovery across worker settings."""

import hashlib
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

import pytest

from hfms_bridge import Transfer, TransferOptions
from hfms_bridge.cli import build_parser
from hfms_bridge.http import HTTPError
from hfms_bridge.state import State
from test_transfer import COMMIT, options, server


@contextmanager
def parallel_server(count=7):
    blobs = {f"part-{i}.bin": bytes([65 + i]) * (1024 * 1024 + i) for i in range(count)}
    digests = {name: hashlib.sha256(blob).hexdigest() for name, blob in blobs.items()}
    names = {sha: name for name, sha in digests.items()}
    mutex = threading.Lock()
    data = dict(
        active_puts=0,
        max_puts=0,
        active_commits=0,
        max_commits=0,
        remote={},
        uploaded=set(),
        puts={},
        commits=[],
        fail_put_once=set(),
        get_payloads=0,
        put_hook=lambda name: None,
        commit_hook=lambda name: None,
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, value, status=200):
            body = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/api/models/alice/source":
                self.reply({"private": False})
            elif "/revision/" in self.path:
                self.reply({"sha": COMMIT})
            elif "/tree/" in self.path:
                self.reply(
                    [
                        {
                            "type": "file",
                            "path": name,
                            "size": len(blob),
                            "lfs": {"oid": digests[name]},
                        }
                        for name, blob in blobs.items()
                    ]
                )
            elif "/resolve/" in self.path:
                with mutex:
                    data["get_payloads"] += 1
                self.reply(blobs[unquote(self.path.rsplit("/", 1)[1])])
            elif self.path.endswith("/revisions"):
                self.reply({"Data": {"RevisionMap": {"Branches": [{"Revision": "master"}]}}})
            elif "/repo/files" in self.path:
                with mutex:
                    files = [dict(Path=name, **value) for name, value in data["remote"].items()]
                self.reply({"Data": {"Files": files}})
            else:
                self.reply({"Success": True, "Data": {}})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path.endswith("/batch"):
                obj = dict(body["objects"][0])
                with mutex:
                    reused = obj["oid"] in data["uploaded"]
                if not reused:
                    obj["actions"] = {"upload": {"href": base + "/upload/" + names[obj["oid"]]}}
                self.reply({"Data": {"objects": [obj]}})
            else:
                op = body["actions"][0]
                name = op["path"]
                with mutex:
                    data["active_commits"] += 1
                    data["max_commits"] = max(data["max_commits"], data["active_commits"])
                try:
                    data["commit_hook"](name)
                    with mutex:
                        assert op["sha256"] in data["uploaded"]
                        data["remote"][name] = {"Size": op["size"], "Sha256": op["sha256"]}
                        data["commits"].append(name)
                    self.reply({"Success": True})
                finally:
                    with mutex:
                        data["active_commits"] -= 1

        def do_PUT(self):
            name = self.path.rsplit("/", 1)[1]
            with mutex:
                data["active_puts"] += 1
                data["max_puts"] = max(data["max_puts"], data["active_puts"])
                data["puts"][name] = data["puts"].get(name, 0) + 1
            try:
                data["put_hook"](name)
                body = self.rfile.read(int(self.headers["Content-Length"]))
                assert len(body) == len(blobs[name])
                assert hashlib.sha256(body).hexdigest() == digests[name]
                if name in data["fail_put_once"] and data["puts"][name] == 1:
                    self.reply({}, 503)
                    return
                with mutex:
                    data["uploaded"].add(digests[name])
                self.reply(b"")
            finally:
                with mutex:
                    data["active_puts"] -= 1

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{httpd.server_port}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield base, data
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


@pytest.mark.parametrize("workers", [1, 3])
def test_actual_parallel_puts_serial_commits_and_resume(tmp_path, workers, monkeypatch):
    from hfms_bridge.http import HTTP

    # Every Session must remain owned by one thread and be closed after the job.
    session_threads = {}
    closed = set()
    lock = threading.Lock()
    request = HTTP.request
    close = HTTP.close

    def tracked_request(client, *args, **kwargs):
        with lock:
            session_threads.setdefault(client, set()).add(threading.get_ident())
        return request(client, *args, **kwargs)

    def tracked_close(client):
        with lock:
            closed.add(client)
        close(client)

    monkeypatch.setattr(HTTP, "request", tracked_request)
    monkeypatch.setattr(HTTP, "close", tracked_close)
    with parallel_server() as (base, data):
        # First N uploads cannot finish unless all N enter concurrently.
        barrier = threading.Barrier(workers, timeout=10)
        data["put_hook"] = lambda name: barrier.wait() if int(name[5:-4]) < workers else None
        opt = options(tmp_path, base, workers=workers)
        assert Transfer(opt, "ms-secret").run() == {"committed": 7, "skipped": 0}
        assert data["max_puts"] == workers
        assert data["max_commits"] == 1
        assert len(data["commits"]) == 7
        before = data["get_payloads"]
        opt.workers = 2 if workers == 1 else 1
        assert Transfer(opt, "ms-secret").run() == {"committed": 0, "skipped": 7}
        assert data["get_payloads"] == before
        assert all(n == 1 for n in data["puts"].values())
        assert len(list(tmp_path.glob("*.sqlite3"))) == 1
        assert sum(p.stat().st_size for p in tmp_path.iterdir()) < 100_000
    assert all(len(threads) == 1 for threads in session_threads.values())
    assert session_threads.keys() <= closed


def test_uploads_continue_while_commit_waits(tmp_path):
    with parallel_server(count=2) as (base, data):
        committing = threading.Event()
        second_uploaded = threading.Event()

        def put_hook(name):
            if name == "part-1.bin":
                assert committing.wait(10)
                second_uploaded.set()

        def commit_hook(name):
            if name == "part-0.bin":
                committing.set()
                assert second_uploaded.wait(10)

        data.update(put_hook=put_hook, commit_hook=commit_hook)
        assert Transfer(options(tmp_path, base, workers=2), "ms-secret").run()["committed"] == 2
        assert data["max_commits"] == 1


def test_unfinished_serial_checkpoint_resumes_with_threads(tmp_path, monkeypatch):
    with server() as (base, data):
        opt = options(tmp_path, base, workers=1)
        opt.attempts = 2
        data["malformed_batch"] = True
        transfer = Transfer(opt, "ms-secret")
        original_files = transfer.source.files
        monkeypatch.setattr(
            transfer.source,
            "files",
            lambda c: iter(sorted(original_files(c), key=lambda f: f.path)),
        )
        monkeypatch.setattr("hfms_bridge.transfer.Transfer._backoff", lambda self, _: None)
        with pytest.raises(Exception, match="LFS batch"):
            transfer.run()
        state_path = next(tmp_path.glob("*.sqlite3"))
        with State(state_path) as state:
            assert state.file("README.md")[2] == "committed"
            assert state.get("complete") is False
            identity = state.get("identity")
        before = data["reads"]
        data["source_commit"] = "b" * 40  # Must retain the unfinished pinned version.
        data["malformed_batch"] = False
        opt.workers = 4
        opt.progress = "off"
        assert Transfer(opt, "ms-secret").run() == {"committed": 1, "skipped": 1}
        assert data["reads"] == before + 1
        assert list(tmp_path.glob("*.sqlite3")) == [state_path]
        with State(state_path) as state:
            assert state.get("identity") == identity
            assert "workers" not in identity
            assert state.get("source_commit") == COMMIT
            assert state.get("complete") is True


def test_failed_parallel_upload_can_resume_serially(tmp_path):
    with server() as (base, data):
        opt = options(tmp_path, base, workers=2)
        data["fail_put"] = True
        with pytest.raises(HTTPError):
            Transfer(opt, "ms-secret").run()
        already = len(data["remote"])
        assert "weights.bin" not in data["remote"]
        data["fail_put"] = False
        opt.workers = 1
        assert Transfer(opt, "ms-secret").run() == {"committed": 2 - already, "skipped": already}
        assert data["puts"] == 2


def test_worker_options_and_cache_identity(tmp_path):
    assert build_parser().parse_args(["hf2ms", "a/b", "c/d", "--ms-token", "x"]).workers == 4
    assert TransferOptions("a/b", "c/d").workers == 4
    for invalid in [0, -1, 1.5, True]:
        with pytest.raises(ValueError, match="workers"):
            TransferOptions("a/b", "c/d", workers=invalid)
    first = Transfer(options(tmp_path, "http://localhost", workers=1), "secret")
    other = Transfer(options(tmp_path, "http://localhost", workers=8), "secret")
    try:
        assert first._identity() == other._identity()
    finally:
        for transfer in (first, other):
            transfer.source.close()
            transfer.target.close()


def test_failure_stops_new_files_but_drains_active_worker(tmp_path, monkeypatch):
    import logging
    from types import SimpleNamespace

    from hfms_bridge.http import TransferError
    from hfms_bridge.hubs import File
    from hfms_bridge.state import Cache

    barrier = threading.Barrier(2, timeout=10)
    release = threading.Event()
    observed_failure = threading.Event()
    started = []
    errors = []
    closed = []
    enumerated = []

    class FailureSignal(logging.Handler):
        def emit(self, record):
            if "finishing active files" in record.getMessage():
                observed_failure.set()

    def files(commit):
        for index in range(1000):
            enumerated.append(index)
            yield File(str(index), 1)

    def process(file, commit, state, exists):
        started.append(file.path)
        barrier.wait()
        if file.path == "0":
            raise TransferError("broken file")
        assert release.wait(10)
        state.mark(file.path, 1, "a" * 64, "committed")
        return "committed"

    transfer = Transfer(options(tmp_path, "http://localhost", workers=2), "secret")
    monkeypatch.setattr(transfer, "_selected", files)
    monkeypatch.setattr(
        transfer,
        "_new_worker",
        lambda: SimpleNamespace(
            _process_file=process,
            source=SimpleNamespace(close=lambda: closed.append("source")),
            target=SimpleNamespace(close=lambda: closed.append("target")),
        ),
    )
    signal = FailureSignal()
    logging.getLogger("hfms-bridge").addHandler(signal)
    try:
        with State(tmp_path / "test-state") as state:

            def run():
                try:
                    transfer._run_files(COMMIT, state, Cache(tmp_path, 1024**2), True)
                except BaseException as exc:
                    errors.append(exc)

            runner = threading.Thread(target=run)
            runner.start()
            try:
                assert observed_failure.wait(10)
                assert runner.is_alive()  # Must wait for the other active file.
                assert set(started) == {"0", "1"}
                assert enumerated == [0, 1]  # Bounded queue, not 1000 queued futures.
            finally:
                release.set()
                runner.join(10)
            assert not runner.is_alive()
            assert len(errors) == 1 and str(errors[0]) == "broken file"
            assert state.file("1")[2] == "committed"
            assert state.file("0") is None
        assert sorted(closed) == ["source", "source", "target", "target"]
    finally:
        logging.getLogger("hfms-bridge").removeHandler(signal)
        transfer.source.close()
        transfer.target.close()
