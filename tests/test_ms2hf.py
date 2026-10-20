"""Real HTTP transport coverage for the reverse direction and shared engine."""

import base64
import hashlib
import io
import json
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from hfms_bridge import Transfer, TransferOptions
from hfms_bridge.cli import build_parser
from hfms_bridge.hubs import File
from hfms_bridge.hubs.hf import RegularCommit
from hfms_bridge.http import CheckedStream, HTTPError, TransferError
from hfms_bridge.state import State

REV = "a" * 40
BODY = b"0123456789abcdef" * (128 * 1024)
FILES = {"README.md": b"# Example\n", "weights.pt": BODY, "notes.jsonl": BODY + b"\n"}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git_sha(data):
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


@contextmanager
def server(kind="dataset", multipart=True, padded=True, part_chunk=262145):
    data = dict(
        remote={},
        blobs={},
        parts={},
        source_calls={},
        ranges=[],
        commits=0,
        puts=0,
        exists=True,
        created=0,
        revision=REV,
        drop=False,
        fail_commit=False,
        modes={},
        multipart=multipart,
        paths=list(FILES),
        private=False,
        marker=0,
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, value, status=200, headers=None):
            body = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def body(self):
            return self.rfile.read(int(self.headers["Content-Length"]))

        def do_GET(self):
            url = urlparse(self.path)
            path, q = url.path, parse_qs(url.query)
            if path.startswith("/api/v1/"):
                prefix = f"/api/v1/{kind}s/alice/source"
                if not path.startswith(prefix):
                    return self.reply({}, 404)
                if path == prefix:
                    return self.reply(
                        {"Data": {"Visibility": 1 if data["private"] else 5, "License": "mit"}}
                    )
                if path.endswith(("/tree", "/files")):
                    entries = [
                        {
                            "Path": p,
                            "Type": "blob",
                            "Size": len(FILES[p]),
                            "Sha256": sha(FILES[p]),
                            "Revision": data["revision"],
                            "IsLFS": p == "weights.pt",
                        }
                        for p in data["paths"]
                    ]
                    if int(q.get("PageNumber", ["1"])[0]) > 1:
                        entries = []
                    return self.reply(
                        {
                            "Data": {
                                "Files": entries,
                                "LatestCommitter": {
                                    "ShortId": data["revision"][:8],
                                    "Marker": data["marker"],
                                },
                            }
                        }
                    )
                if path.endswith("/repo"):
                    name = q["FilePath"][0]
                    # A later branch revision must never be used by an unfinished snapshot.
                    data.setdefault("download_revisions", []).append(q["Revision"][0])
                    content = FILES[name]
                    calls = data["source_calls"].get(name, 0) + 1
                    data["source_calls"][name] = calls
                    if self.headers.get("Range"):
                        start = int(self.headers["Range"][6:-1])
                        data["ranges"].append(start)
                        return self.reply(
                            content[start:],
                            206,
                            {"Content-Range": f"bytes {start}-{len(content) - 1}/{len(content)}"},
                        )
                    if data["drop"] and name == "weights.pt" and calls >= 2:
                        data["drop"] = False
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(content)))
                        self.end_headers()
                        self.wfile.write(content[:100000])
                        self.wfile.flush()
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                    return self.reply(content)
            if path == f"/api/{kind}s/bob/dest":
                return self.reply(
                    {"private": False} if data["exists"] else {}, 200 if data["exists"] else 404
                )
            if path == f"/api/{kind}s/bob/dest/refs":
                return self.reply({"branches": [{"name": "main"}]})
            self.reply({}, 404)

        def do_POST(self):
            path = urlparse(self.path).path
            assert self.headers.get("Authorization") == "Bearer hf-secret" or path == "/complete"
            raw = self.body()
            if "/commit/" in path:
                data["commits"] += 1
                if data["fail_commit"]:
                    return self.reply({}, 503)
                header, op = map(json.loads, raw.splitlines())
                assert header["key"] == "header"
                value = op["value"]
                if op["key"] == "lfsFile":
                    content = data["blobs"][value["oid"]]
                    assert len(content) == value["size"]
                    metadata = {"lfs": {"oid": sha(content)}, "oid": "b" * 40}
                else:
                    content = base64.b64decode(value["content"], validate=True)
                    metadata = {"oid": git_sha(content)}
                assert content == FILES[value["path"]]
                data["remote"][value["path"]] = dict(
                    metadata, size=len(content), path=value["path"], type="file"
                )
                return self.reply({"commitOid": "c" * 40})
            if "/paths-info/" in path:
                assert self.headers["Content-Type"] == "application/x-www-form-urlencoded"
                body = parse_qs(raw.decode())
                return self.reply([data["remote"][p] for p in body["paths"] if p in data["remote"]])
            body = json.loads(raw)
            if path == "/api/repos/create":
                data["created"] += 1
                data["exists"] = True
                data["create_body"] = body
                return self.reply({"url": base + "/bob/dest"})
            if "/preupload/" in path:
                f = body["files"][0]
                assert base64.b64decode(f["sample"]) == FILES[f["path"]][:512]
                mode = data["modes"].get(
                    f["path"], "lfs" if f["path"] == "weights.pt" else "regular"
                )
                return self.reply(
                    {"files": [{"path": f["path"], "uploadMode": mode, "shouldIgnore": False}]}
                )
            if path.endswith("/info/lfs/objects/batch"):
                assert body["transfers"] == ["basic", "multipart"]
                obj = body["objects"][0]
                if obj["oid"] not in data["blobs"]:
                    if data["multipart"]:
                        chunk = part_chunk
                        count = (obj["size"] + chunk - 1) // chunk
                        action = {
                            "href": base + "/complete",
                            "header": {
                                "chunk_size": str(chunk),
                                **{
                                    (f"{n:05d}" if padded else str(n)): base + f"/part/{n}"
                                    for n in reversed(range(1, count + 1))
                                },
                            },
                        }
                    else:
                        action = {"href": base + "/upload"}
                    obj["actions"] = {"upload": action}
                return self.reply({"objects": [obj]})
            if path == "/complete":
                for part in body["parts"]:
                    assert part["etag"] == f'"part-{part["partNumber"]}"'
                content = b"".join(data["parts"][n] for n in sorted(data["parts"]))
                assert sha(content) == body["oid"]
                data["blobs"][body["oid"]] = content
                return self.reply({})
            self.reply({}, 404)

        def do_PUT(self):
            data["puts"] += 1
            body = self.body()
            assert "Transfer-Encoding" not in self.headers
            if self.path.startswith("/part/"):
                number = int(self.path.rsplit("/", 1)[1])
                data["parts"][number] = body
                return self.reply(b"", headers={"ETag": f'"part-{number}"'})
            data["blobs"][sha(body)] = body
            self.reply(b"")

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{http.server_port}"
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield base, data
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


def options(tmp_path, base, **kw):
    return TransferOptions(
        "alice/source",
        "bob/dest",
        direction="ms2hf",
        cache_dir=tmp_path,
        hf_endpoint=base,
        ms_endpoint=base,
        progress="off",
        **kw,
    )


@pytest.mark.parametrize("kind", ["model", "dataset"])
@pytest.mark.parametrize("multipart", [False, True])
def test_transfer_and_skip_identical(tmp_path, kind, multipart):
    with server(kind, multipart) as (base, data):
        opt = options(tmp_path, base, workers=3)
        assert Transfer(opt, hf_token="hf-secret").run() == {"committed": 3, "skipped": 0}
        puts, commits = data["puts"], data["commits"]
        assert Transfer(opt, hf_token="hf-secret").run() == {"committed": 0, "skipped": 3}
        assert (data["puts"], data["commits"]) == (puts, commits)
        assert all(r == REV for r in data["download_revisions"])
        assert sum(p.stat().st_size for p in tmp_path.iterdir()) < 100000


@pytest.mark.parametrize("multipart", [False, True])
def test_range_recovery_preserves_upload(tmp_path, monkeypatch, multipart):
    with server(multipart=multipart) as (base, data):
        data.update(drop=True, paths=["weights.pt"])
        monkeypatch.setattr(Transfer, "_backoff", lambda *args: None)
        assert Transfer(options(tmp_path, base), hf_token="hf-secret").run()["committed"] == 1
        assert data["ranges"] and data["ranges"][0] > 0
        assert data["puts"] == (8 if multipart else 1)
        assert data["commits"] == 1


def test_resume_snapshot_after_source_branch_changes(tmp_path, monkeypatch):
    with server() as (base, data):
        data.update(fail_commit=True, paths=["weights.pt"])
        opt = options(tmp_path, base, workers=1, attempts=1)
        with pytest.raises(HTTPError):
            Transfer(opt, hf_token="hf-secret").run()
        statepath = next(tmp_path.glob("*.sqlite3"))
        with State(statepath) as state:
            pinned = state.get("source_commit")
            assert pinned.startswith("ms-snapshot:")
        puts = data["puts"]
        data.update(fail_commit=False, revision="b" * 40, paths=["README.md"])
        opt.workers = 3
        assert Transfer(opt, hf_token="hf-secret").run() == {"committed": 1, "skipped": 0}
        assert data["puts"] == puts
        assert "README.md" not in data["remote"]
        assert all(r == REV for r in data["download_revisions"])
        with State(statepath) as state:
            assert state.get("source_commit") == pinned
            assert state.get("complete")


def test_dry_run_and_create_private(tmp_path):
    with server() as (base, data):
        data.update(exists=False, private=True)
        opt = options(tmp_path, base, dry_run=True)
        report = Transfer(opt, hf_token="hf-secret").run()
        assert report["create_repo"] and report["visibility"] == "private"
        assert not list(tmp_path.iterdir())
        assert not data["source_calls"] and not data["created"]
        opt.dry_run = False
        Transfer(opt, hf_token="hf-secret").run()
        assert data["created"] == 1 and data["create_body"]["private"] is True


def test_cli_and_direction_identity(tmp_path):
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["ms2hf", "a/b", "c/d"])
    args = parser.parse_args(["ms2hf", "a/b", "c/d", "--hf-token", "hf"])
    assert args.direction == "ms2hf"
    assert args.visibility == "auto"
    first = Transfer(TransferOptions("a/b", "c/d"), ms_token="ms")
    second = Transfer(TransferOptions("a/b", "c/d", direction="ms2hf"), hf_token="hf")
    try:
        assert first._identity() != second._identity()
        assert second._new_worker().target.token == "hf"
    finally:
        for t in [first, second]:
            t.source.close()
            t.target.close()


@pytest.mark.parametrize("size", [0, 1, 2, 3, 65537, 1048583])
def test_regular_commit_base64_boundaries(size):
    content = b"x" * size
    stream = CheckedStream(io.BytesIO(content), size, sha(content), 4097)
    body = RegularCommit(File('dir/"file.jsonl', size), stream)
    chunks = []
    while chunk := body.read(1231):
        assert len(chunk) <= 1231
        chunks.append(chunk)
    encoded = b"".join(chunks)
    assert len(encoded) == len(body)
    header, file = map(json.loads, encoded.splitlines())
    assert base64.b64decode(file["value"]["content"], validate=True) == content
    assert file["value"]["path"] == 'dir/"file.jsonl'


def test_snapshot_rolls_back_if_listing_fails(tmp_path):
    with State(tmp_path / "state.sqlite3") as state:
        old = state.snapshot([File("a", 1, sha(b"a"), REV)])

        def broken():
            yield File("b", 1, sha(b"b"), "b" * 40)
            raise TransferError("listing interrupted")

        with pytest.raises(TransferError):
            state.snapshot(broken())
        assert state.get("source_commit") == old
        assert [f.path for f in state.source_files()] == ["a"]


@pytest.mark.parametrize("objects", [None, [], [None], [{"oid": "bad", "size": 1}]])
def test_invalid_hf_batch_is_retryable(monkeypatch, objects):
    from hfms_bridge.hubs import HuggingFace
    from hfms_bridge.http import RetryableTransferError

    target = HuggingFace("a/b", "model", "secret")
    try:
        monkeypatch.setattr(target, "_api", lambda *a, **kw: {"objects": objects})
        with pytest.raises(RetryableTransferError):
            target.negotiate(sha(b"x"), 1)
    finally:
        target.close()


@pytest.mark.parametrize(
    "headers",
    [
        {"chunk_size": "0"},
        {"chunk_size": "x"},
        {"chunk_size": "2", "2": "https://storage.test/part"},
    ],
)
def test_invalid_multipart_does_not_read_source(headers):
    from hfms_bridge.hubs import HuggingFace
    from hfms_bridge.http import RetryableTransferError

    target = HuggingFace("a/b", "model", "secret")
    source = io.BytesIO(b"abcd")
    try:
        with pytest.raises(RetryableTransferError):
            target.upload(
                {"upload": {"href": "https://storage.test/complete", "header": headers}},
                source,
                sha(b"abcd"),
                4,
            )
        assert source.tell() == 0
    finally:
        target.close()


def test_dry_run_preserves_unfinished_snapshot(tmp_path):
    with server() as (base, data):
        data.update(paths=["weights.pt"], fail_commit=True)
        opt = options(tmp_path, base, attempts=1)
        with pytest.raises(HTTPError):
            Transfer(opt, hf_token="hf-secret").run()
        statepath = next(tmp_path.glob("*.sqlite3"))
        before = statepath.read_bytes()
        calls = dict(data["source_calls"])
        data.update(revision="b" * 40, paths=["README.md"])
        opt.dry_run = True
        report = Transfer(opt, hf_token="hf-secret").run()
        assert report["files"] == 1 and report["bytes"] == len(BODY)
        assert statepath.read_bytes() == before
        assert data["source_calls"] == calls


def test_snapshot_budget_stops_listing_and_preserves_previous_snapshot(tmp_path):
    with State(tmp_path / "state.sqlite3") as state:
        old = state.snapshot([File("a", 1, sha(b"a"), REV)])
        state.max_size = 65536
        visited = []

        def many_files():
            for index in range(10000):
                visited.append(index)
                yield File(f"directory/{index:06d}.pt", 1, sha(b"b"), "b" * 40)

        with pytest.raises(TransferError, match="snapshot exceeds cache budget"):
            state.snapshot(many_files())
        assert len(visited) < 1000
        assert state.get("source_commit") == old
        assert [f.path for f in state.source_files()] == ["a"]
        assert state.path.stat().st_size <= state.max_size


def test_hf_mirror_destination_routes_writes_and_resumes_existing_state(tmp_path, monkeypatch):
    from hfms_bridge.http import HTTP
    from hfms_bridge.state import Cache

    with server() as (base, data):
        opt = options(tmp_path, base, repo_type="dataset", workers=3)
        opt.hf_endpoint = "https://hf-mirror.com"
        transfer = Transfer(opt, hf_token="hf-secret")
        identity = transfer._identity()
        assert identity["hf_endpoint"] == "https://hf-mirror.com"
        cache = Cache(tmp_path, opt.cache_max_size)
        with cache.open(identity) as state:
            pinned = state.snapshot(
                File(path, len(content), sha(content), REV, path == "weights.pt")
                for path, content in FILES.items()
            )
        # The branch has changed since this task's original, mirror-configured run.
        data.update(revision="b" * 40, paths=["README.md"])
        request = HTTP.request
        official_calls = []

        def route(self, method, url, **kwargs):
            assert not url.startswith("https://hf-mirror.com")
            if url.startswith("https://huggingface.co/"):
                official_calls.append((method, url))
                url = base + url.removeprefix("https://huggingface.co")
            return request(self, method, url, **kwargs)

        monkeypatch.setattr(HTTP, "request", route)
        # Covers small inline, large regular streaming, and LFS commits in workers.
        assert transfer.run() == {"committed": 3, "skipped": 0}
        assert len([url for method, url in official_calls if "/commit/" in url]) == 3
        assert all(r == REV for r in data["download_revisions"])
        with State(cache.path(identity)) as state:
            assert state.get("source_commit") == pinned
            assert state.get("complete")


def test_hf_source_still_uses_configured_mirror():
    transfer = Transfer(
        TransferOptions("a/b", "c/d", hf_endpoint="https://hf-mirror.com"), ms_token="secret"
    )
    try:
        assert transfer.source.endpoint == "https://hf-mirror.com"
    finally:
        transfer.source.close()
        transfer.target.close()


@pytest.mark.parametrize("padded", [False, True])
def test_multipart_part_keys_preserve_urls_and_numeric_order(tmp_path, padded):
    with server(padded=padded, part_chunk=131073) as (base, data):
        data["paths"] = ["weights.pt"]
        assert Transfer(options(tmp_path, base), hf_token="hf-secret").run() == {
            "committed": 1,
            "skipped": 0,
        }
        assert data["blobs"][sha(BODY)] == BODY
        assert data["puts"] == 16


def test_multipart_duplicate_numeric_keys_are_rejected():
    from hfms_bridge.hubs import HuggingFace
    from hfms_bridge.http import RetryableTransferError

    target = HuggingFace("a/b", "model", "secret")
    source = io.BytesIO(b"abcd")
    try:
        with pytest.raises(RetryableTransferError, match="Invalid HF multipart instructions"):
            target.upload(
                {
                    "upload": {
                        "href": "https://storage.test/complete",
                        "header": {
                            "chunk_size": "2",
                            "1": "https://storage.test/first",
                            "00001": "https://storage.test/duplicate",
                        },
                    }
                },
                source,
                sha(b"abcd"),
                4,
            )
        assert source.tell() == 0
    finally:
        target.close()
