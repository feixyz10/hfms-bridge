import hashlib
import io
import json
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hfms_bridge import Transfer, TransferOptions
from hfms_bridge.http import CheckedStream, HTTPError, RetryableTransferError, TransferError
from hfms_bridge.hubs import ModelScope
from hfms_bridge.state import State

COMMIT = "a" * 40
BLOB = b"abcdefgh" * (256 * 1024)
SHA = hashlib.sha256(BLOB).hexdigest()


@contextmanager
def server():
    data = dict(
        reads=0,
        puts=0,
        commits=0,
        blob=False,
        fail_commit=False,
        fail_put=False,
        bad_source=False,
        omit_hash=False,
        drop_put=False,
        requests=[],
        remote={},
        exists=True,
        created=0,
        hf_types=("model", "dataset"),
        hf_private=False,
        hf_gated=False,
        source_commit=COMMIT,
        branches=["master"],
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, value, status=200, headers=None):
            body = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            data["requests"].append(self.path)
            if self.path in ("/api/models/alice/source", "/api/datasets/alice/source"):
                kind = "dataset" if "/datasets/" in self.path else "model"
                if kind not in data["hf_types"]:
                    self.reply({}, 404)
                else:
                    self.reply(
                        {
                            "private": data["hf_private"],
                            "gated": data["hf_gated"],
                            "cardData": {"license": "mit"},
                        }
                    )
            elif "/revision/" in self.path:
                self.reply({"sha": data["source_commit"]})
            elif "/tree/" in self.path:
                if "page=2" in self.path:
                    self.reply([{"type": "file", "path": "README.md", "size": 5}])
                else:
                    link = base + self.path + "&page=2"
                    self.reply(
                        [
                            {
                                "type": "file",
                                "path": "weights.bin",
                                "size": len(BLOB),
                                "lfs": {} if data["omit_hash"] else {"oid": SHA},
                            }
                        ],
                        headers={"Link": "<" + link + '>; rel="next"'},
                    )
            elif "/resolve/" in self.path:
                assert "/resolve/" in self.path
                data["reads"] += 1
                blob = BLOB if self.path.endswith("weights.bin") else b"hello"
                if data["bad_source"]:
                    blob = b"x" * len(blob)
                if self.path.endswith("weights.bin"):
                    range_header = self.headers.get("Range")
                    data.setdefault("source_ranges", []).append(range_header)
                    if data.get("drop_source") and not range_header:
                        data["drop_source"] = False
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(blob)))
                        self.end_headers()
                        self.wfile.write(blob[:100000])
                        self.wfile.flush()
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                    if range_header and not data.get("ignore_range"):
                        start = int(range_header.removeprefix("bytes=").removesuffix("-"))
                        headers = {
                            "Content-Range": f"bytes {start}-{len(blob) - 1}/{len(blob)}",
                        }
                        if data.get("encoded_range"):
                            headers["Content-Encoding"] = "gzip"
                        self.reply(blob[start:], 206, headers)
                        return
                self.reply(blob)
            elif self.path.startswith("/api/v1/"):
                if self.path.endswith("/revisions"):
                    if data.get("revisions_missing"):
                        self.reply({}, 404)
                        return
                    self.reply(
                        {
                            "Data": {
                                "RevisionMap": {
                                    "Branches": [{"Revision": b} for b in data["branches"]],
                                    "Tags": [],
                                }
                            }
                        }
                    )
                elif "/repo/files" in self.path or "/repo/tree" in self.path:
                    self.reply(
                        {"Data": {"Files": [dict(Path=p, **f) for p, f in data["remote"].items()]}}
                    )
                else:
                    self.reply({"Success": True, "Data": {}}, 200 if data["exists"] else 404)
            else:
                self.reply({}, 404)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path in ("/api/v1/models", "/api/v1/datasets"):
                data["created"] += 1
                data["exists"] = True
                data["create_body"] = raw
                self.reply({"Success": True, "Data": {}})
                return
            body = json.loads(raw)
            assert self.headers["Authorization"] == "Bearer ms-secret"
            if self.path.endswith("/batch"):
                data["batches"] = data.get("batches", 0) + 1
                if data.get("malformed_batch"):
                    self.reply({"Data": {}})
                    return
                if data.get("legacy_reuse") and data["blob"]:
                    self.reply(
                        {
                            "Code": 200,
                            "Success": True,
                            "Data": {"objects": [], "reused-objects": []},
                        }
                    )
                    return
                obj = dict(body["objects"][0])
                if not data["blob"]:
                    obj["actions"] = {"upload": {"href": base + "/upload"}}
                self.reply({"Data": {"objects": [obj]}})
            else:
                data["commits"] += 1
                if data["fail_commit"]:
                    self.reply({}, 503)
                else:
                    data.setdefault("operations", []).extend(body["actions"])
                    import base64

                    for op in body["actions"]:
                        digest = (
                            op["sha256"]
                            or hashlib.sha256(base64.b64decode(op["content"])).hexdigest()
                        )
                        data["remote"][op["path"]] = {
                            "Size": op["size"],
                            "Sha256": digest,
                            "Type": "blob",
                        }
                    self.reply({"Success": True, "Data": {"CommitId": "abc"}})

        def do_PUT(self):
            data["puts"] += 1
            if data["drop_put"]:
                self.rfile.read(1024)
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            assert "Transfer-Encoding" not in self.headers
            assert int(self.headers["Content-Length"]) == len(BLOB)
            uploaded = self.rfile.read(int(self.headers["Content-Length"]))
            if len(uploaded) != len(BLOB):
                return
            assert hashlib.sha256(uploaded).hexdigest() == SHA
            if data["fail_put"]:
                self.reply({}, 503)
            else:
                data["blob"] = True
                self.reply(b"")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = "http://127.0.0.1:{}".format(httpd.server_port)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield base, data
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def options(tmp_path, base, **kwargs):
    return TransferOptions(
        "alice/source",
        "bob/dest",
        cache_dir=tmp_path,
        repo_type=kwargs.pop("repo_type", "model"),
        hf_endpoint=base,
        ms_endpoint=base,
        attempts=1,
        workers=kwargs.pop("workers", 1),
        **kwargs,
    )


@pytest.mark.parametrize("repo_type", ["model", "dataset"])
def test_actual_http_upload_and_restart(tmp_path, repo_type):
    with server() as (base, data):
        opt = options(tmp_path, base, repo_type=repo_type)
        assert Transfer(opt, "ms-secret").run() == {"committed": 2, "skipped": 0}
        assert data["puts"] == 1
        assert data["operations"][1]["content"] == "aGVsbG8="
        reads = data["reads"]
        assert Transfer(opt, "ms-secret").run() == {"committed": 0, "skipped": 2}
        assert data["reads"] == reads
        assert data["puts"] == 1
        assert sum("/revision/" in p for p in data["requests"]) == 2
        assert all(p.stat().st_size < 100_000 for p in tmp_path.iterdir())


@pytest.mark.parametrize("resume_workers", [1, 4])
def test_blob_survives_commit_failure(tmp_path, resume_workers):
    with server() as (base, data):
        opt = options(tmp_path, base)
        data["fail_commit"] = True
        with pytest.raises(HTTPError):
            Transfer(opt, "ms-secret").run()
        assert data["blob"]
        with State(next(tmp_path.glob("*.sqlite3"))) as state:
            assert state.file("weights.bin")[2] == "uploaded"
        data["fail_commit"] = False
        opt.workers = resume_workers
        Transfer(opt, "ms-secret").run()
        assert data["puts"] == 1
        assert data["reads"] == 2  # weights once, README once


def test_incomplete_upload_restarts_file(tmp_path):
    with server() as (base, data):
        opt = options(tmp_path, base)
        data["fail_put"] = True
        with pytest.raises(HTTPError):
            Transfer(opt, "ms-secret").run()
        assert not data["blob"]
        assert data["commits"] == 0
        data["fail_put"] = False
        Transfer(opt, "ms-secret").run()
        assert data["puts"] == 2


def test_restart_reuses_remote_blob(tmp_path):
    with server() as (base, data):
        opt = options(tmp_path, base)
        Transfer(opt, "ms-secret").run()
        opt.restart = True
        Transfer(opt, "ms-secret").run()
        assert data["puts"] == 1
        assert data["commits"] == 2


def test_hash_pass_without_lfs_metadata(tmp_path):
    with server() as (base, data):
        data["omit_hash"] = True
        Transfer(options(tmp_path, base), "ms-secret").run()
        assert data["reads"] == 3  # hash pass, upload pass, README
        assert data["puts"] == 1


def test_actual_socket_disconnect_and_resume(tmp_path):

    with server() as (base, data):
        opt = options(tmp_path, base)
        data["drop_put"] = True
        with pytest.raises(RetryableTransferError, match="MS upload: network"):
            Transfer(opt, "ms-secret").run()
        assert not data["blob"]
        assert data["commits"] == 0
        data["drop_put"] = False
        Transfer(opt, "ms-secret").run()
        assert data["puts"] == 2


def test_corrupt_source_cannot_commit(tmp_path):
    with server() as (base, data):
        data["bad_source"] = True
        with pytest.raises(TransferError, match="SHA-256"):
            Transfer(options(tmp_path, base), "ms-secret").run()
        assert data["commits"] == 0
        assert not data["blob"]


def test_filters(tmp_path):
    with server() as (base, data):
        opt = options(tmp_path, base, exclude=("*.bin",))
        assert Transfer(opt, "ms-secret").run()["committed"] == 1
        assert data["puts"] == 0
        assert data["reads"] == 1


def test_retry_renegotiates_instead_of_replaying_body(tmp_path, monkeypatch):
    with server() as (base, data):
        opt = options(tmp_path, base)
        opt.attempts = 2
        data["fail_put"] = True
        monkeypatch.setattr(
            "hfms_bridge.transfer.Transfer._backoff", lambda self, _: data.update(fail_put=False)
        )
        Transfer(opt, "ms-secret").run()
        assert data["puts"] == 2
        assert data["reads"] == 3


def test_hash_checked_before_last_bytes():
    stream = CheckedStream(io.BytesIO(b"bad"), 3, hashlib.sha256(b"yes").hexdigest())
    with pytest.raises(TransferError, match="SHA-256"):
        stream.read()


@pytest.mark.parametrize("body,size", [(b"ab", 3), (b"abcd", 3)])
def test_wrong_length(body, size):
    stream = CheckedStream(io.BytesIO(body), size, "", 1)
    with pytest.raises(TransferError):
        while stream.read():
            pass


def test_bounded_reads_and_memory():
    import tracemalloc

    class Generated:
        remaining = 32 * 1024 * 1024

        def read(self, n):
            assert 0 <= n <= 65536
            n = min(n, self.remaining)
            self.remaining -= n
            return b"a" * n

    stream = CheckedStream(Generated(), 32 * 1024 * 1024, "", 65536)
    tracemalloc.start()
    try:
        while stream.read():
            pass
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1024 * 1024
    stream.verify()


def test_state_identity_and_exclusive_lock(tmp_path):
    from filelock import Timeout

    with State(tmp_path / "state") as state:
        state.bind({"repo": "a"})
        with pytest.raises(ValueError):
            state.bind({"repo": "b"})
        with pytest.raises(Timeout):
            with State(tmp_path / "state"):
                pass


def test_tokens_not_forwarded_to_storage():
    target = ModelScope("a/b", "model", "secret")
    try:
        assert target._action_headers({"href": "https://bucket.example/upload"}) == {}
        assert "Authorization" in target._action_headers({"href": "https://lfs.modelscope.cn/u"})
        assert not target._action_headers({"href": "https://modelscope.cn.evil.example/u"})
    finally:
        target.close()


def test_missing_blob_response_is_not_reuse(monkeypatch):
    target = ModelScope("a/b", "model", "secret")
    try:
        monkeypatch.setattr(target, "_api", lambda *a, **k: {})
        with pytest.raises(TransferError):
            target.negotiate(SHA, len(BLOB))
    finally:
        target.close()


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"objects": None},
        {"objects": [None]},
        {"objects": [{"oid": "f" * 64}]},
        {"objects": [{"oid": SHA}, {"oid": SHA}]},
    ],
)
def test_invalid_batch_is_retryable(response, monkeypatch):
    from hfms_bridge.http import RetryableTransferError

    target = ModelScope("owner/repo", "dataset", "secret", "master")
    monkeypatch.setattr(target, "_api", lambda *args, **kwargs: response)
    try:
        with pytest.raises(RetryableTransferError):
            target.negotiate(SHA, len(BLOB))
    finally:
        target.close()


def test_malformed_batch_retries_then_succeeds(tmp_path, monkeypatch):
    with server() as (base, data):
        opt = options(tmp_path, base)
        opt.attempts = 3
        data["malformed_batch"] = True
        monkeypatch.setattr(
            "hfms_bridge.transfer.Transfer._backoff", lambda self, _: data.update(malformed_batch=False)
        )
        result = Transfer(opt, "ms-secret").run()
        assert result["committed"] == 2
        assert data["batches"] == 2
        assert data["puts"] == 1


def test_malformed_batch_exhaustion_preserves_resume(tmp_path, monkeypatch):
    from hfms_bridge.http import RetryableTransferError

    with server() as (base, data):
        opt = options(tmp_path, base)
        opt.attempts = 3
        data["malformed_batch"] = True
        monkeypatch.setattr("hfms_bridge.transfer.Transfer._backoff", lambda self, _: None)
        transfer = Transfer(opt, "ms-secret")
        original_files = transfer.source.files
        monkeypatch.setattr(
            transfer.source,
            "files",
            lambda commit: iter(sorted(original_files(commit), key=lambda f: f.path)),
        )
        with pytest.raises(RetryableTransferError):
            transfer.run()
        assert data["batches"] == 3
        assert data["puts"] == 0
        assert data["commits"] == 1
        data["malformed_batch"] = False
        result = Transfer(opt, "ms-secret").run()
        assert result == {"committed": 1, "skipped": 1}
        assert data["commits"] == 2
        assert data["puts"] == 1


@pytest.mark.parametrize("repo_type", ["model", "dataset"])
def test_legacy_empty_batch_reuses_blob_and_commits(tmp_path, repo_type):
    with server() as (base, data):
        data.update(blob=True, legacy_reuse=True)
        result = Transfer(options(tmp_path, base, repo_type=repo_type), "ms-secret").run()
        assert result == {"committed": 2, "skipped": 0}
        assert data["puts"] == 0
        assert data["reads"] == 1  # README only; LFS content was not downloaded.
        assert data["remote"]["weights.bin"]["Sha256"] == SHA


def test_legacy_reused_blob_failed_commit_resumes(tmp_path):
    with server() as (base, data):
        opt = options(tmp_path, base)
        data.update(blob=True, legacy_reuse=True, fail_commit=True)
        with pytest.raises(HTTPError):
            Transfer(opt, "ms-secret").run()
        assert "weights.bin" not in data["remote"]
        assert data["reads"] == data["puts"] == 0
        data["fail_commit"] = False
        result = Transfer(opt, "ms-secret").run()
        assert result == {"committed": 2, "skipped": 0}
        assert data["puts"] == 0
        assert data["remote"]["weights.bin"]["Sha256"] == SHA
