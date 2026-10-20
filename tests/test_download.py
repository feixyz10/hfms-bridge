"""Download transport tests reuse the bridge's actual local HTTP fixtures."""

from dataclasses import asdict

import pytest

from hfms_bridge.download import Download, DownloadOptions
from hfms_bridge.download_cli import build_parser
from hfms_bridge.download_state import DownloadState, read_json, validate_paths, write_json
from hfms_bridge.hubs import File
from hfms_bridge.http import HTTP, TransferError
from test_ms2hf import BODY, FILES, REV, server, sha
from test_transfer import BLOB, server as hf_server


def options(root, base, **kw):
    return DownloadOptions(
        "ms", "alice/source", local_dir=root, endpoint=base, progress="off", **kw
    )


@pytest.mark.parametrize("kind", ["model", "dataset"])
def test_ms_download_and_skip(tmp_path, kind):
    with server(kind=kind) as (base, data):
        root = tmp_path / "repo"
        opt = options(root, base)
        result = Download(opt).run()
        assert result["downloaded"] == 3
        assert {
            f.relative_to(root).as_posix(): f.read_bytes() for f in root.rglob("*") if f.is_file()
        } == FILES
        assert not (root / ".hfms-download").exists()
        assert not list(DownloadState(root).directory.glob("*.incomplete"))
        before = dict(data["source_calls"])
        assert Download(opt).run()["skipped"] == 3
        assert data["source_calls"] == before


def test_hf_download(tmp_path):
    with hf_server() as (base, data):
        root = tmp_path / "hf"
        result = Download(
            DownloadOptions(
                "hf",
                "alice/source",
                local_dir=root,
                endpoint=base,
                progress="off",
                repo_type="model",
            )
        ).run()
        assert result["downloaded"] > 0
        assert any(f.read_bytes() == BLOB for f in root.rglob("*") if f.is_file())


def test_persisted_partial_resumes_after_exit_and_pins_revision(tmp_path, monkeypatch):
    with server() as (base, data):
        root = tmp_path / "repo"
        data["paths"] = ["weights.pt"]
        opt = options(root, base, attempts=1)
        original = HTTP.request
        cut = 65536

        class Interrupted:
            def __init__(self, raw):
                self.raw, self.reads = raw, 0

            def read(self, size):
                if self.reads:
                    raise OSError("simulated process interruption")
                self.reads += 1
                return self.raw.read(min(size, cut))

            def close(self):
                self.raw.close()

        def request(self, method, url, **kw):
            r = original(self, method, url, **kw)
            if "/repo?" in url:
                r.raw = Interrupted(r.raw)
            return r

        monkeypatch.setattr(HTTP, "request", request)
        with pytest.raises(OSError):
            Download(opt).run()
        state = DownloadState(root)
        assert state.paths("weights.pt")[1].stat().st_size == cut
        assert not (root / "weights.pt").exists()
        pinned = read_json(state.manifest_path)["commit"]
        data.update(revision="b" * 40, paths=["README.md"])
        monkeypatch.setattr(HTTP, "request", original)
        assert Download(opt).run()["downloaded"] == 1
        assert data["ranges"] == [cut]
        assert (root / "weights.pt").read_bytes() == BODY
        assert all(r == REV for r in data["download_revisions"])
        assert read_json(state.manifest_path)["commit"] == pinned


def seed_partial(root, opt, size, lfs=True):
    state = DownloadState(root)
    state.prepare()
    name = "weights.pt" if lfs else "notes.jsonl"
    f = File(name, len(FILES[name]), sha(FILES[name]), REV, lfs)
    commit = "ms-snapshot:seed"
    write_json(
        state.manifest_path,
        {
            "schema": 1,
            "identity": {
                "hub": "ms",
                "repo": "alice/source",
                "type": "dataset",
                "revision": "master",
                "endpoint": opt.endpoint,
            },
            "commit": commit,
            "files": [asdict(f)],
            "complete": False,
        },
    )
    meta, partial = state.paths(name)
    write_json(meta, {"pending": dict(asdict(f), source_commit=REV)})
    partial.write_bytes(FILES[name][:size])
    return state, partial


@pytest.mark.parametrize("lfs", [True, False])
def test_partial_recovery_and_regular_ms_restart(tmp_path, lfs):
    with server() as (base, data):
        root = tmp_path / "repo"
        opt = options(root, base)
        seed_partial(root, opt, 65536, lfs)
        assert Download(opt).run()["downloaded"] == 1
        assert bool(data["ranges"]) is lfs


def test_invalid_range_restarts_without_appending(tmp_path, monkeypatch):
    with server() as (base, data):
        root = tmp_path / "repo"
        opt = options(root, base, attempts=2)
        seed_partial(root, opt, 65536)
        original = HTTP.request

        def request(self, method, url, **kw):
            if kw.get("headers", {}).get("Range"):
                kw["headers"].pop("Range")  # Server ignores Range and responds with full HTTP 200.
            return original(self, method, url, **kw)

        monkeypatch.setattr(HTTP, "request", request)
        assert Download(opt).run()["downloaded"] == 1
        assert (root / "weights.pt").read_bytes() == BODY


def test_corrupt_partial_is_replaced(tmp_path):
    with server() as (base, data):
        root = tmp_path / "repo"
        opt = options(root, base, attempts=2)
        _, partial = seed_partial(root, opt, 65536)
        partial.write_bytes(b"x" * 65536)
        assert Download(opt).run()["downloaded"] == 1
        assert (root / "weights.pt").read_bytes() == BODY


def test_existing_local_content_requires_force(tmp_path):
    with server() as (base, data):
        data["paths"] = ["README.md"]
        root = tmp_path / "repo"
        root.mkdir()
        (root / "README.md").write_bytes(b"local edits")
        opt = options(root, base)
        with pytest.raises(TransferError, match="--force-download"):
            Download(opt).run()
        assert (root / "README.md").read_bytes() == b"local edits"
        opt.force_download = True
        Download(opt).run()
        assert (root / "README.md").read_bytes() == FILES["README.md"]


def test_dry_run_does_not_write_or_read_payload(tmp_path):
    with server() as (base, data):
        report = Download(options(tmp_path / "repo", base, dry_run=True)).run()
        assert report["files"] == 3
        assert not list(tmp_path.iterdir())
        assert not data["source_calls"]


def test_filters_and_missing_explicit_file(tmp_path):
    with server() as (base, data):
        opt = options(tmp_path / "repo", base, filenames=("missing",), dry_run=True)
        with pytest.raises(TransferError, match="do not exist"):
            Download(opt).run()
        opt.filenames = ()
        opt.include, opt.exclude = ("*.jsonl", "*.pt"), ("*.pt",)
        assert Download(opt).run()["files"] == 1


@pytest.mark.parametrize("kind,variable", [("model", "HF_MODELS"), ("dataset", "HF_DATASETS")])
def test_preset_validation_and_layout(tmp_path, monkeypatch, kind, variable):
    with server(kind=kind) as (base, data):
        opt = DownloadOptions("ms", "alice/source", preset="author", endpoint=base, dry_run=True)
        monkeypatch.delenv(variable, raising=False)
        with pytest.raises(TransferError, match=variable):
            Download(opt).run()
        monkeypatch.setenv(variable, str(tmp_path / "missing"))
        with pytest.raises(TransferError, match=variable):
            Download(opt).run()
        monkeypatch.setenv(variable, str(tmp_path))
        assert Download(opt).run()["local_dir"] == str(tmp_path / "alice/source")
        assert not list(tmp_path.iterdir())
        direct = options(tmp_path / "direct", base, dry_run=True)
        assert Download(direct).run()["local_dir"] == str(tmp_path / "direct")
        direct.layout = "repo"
        assert Download(direct).run()["local_dir"] == str(tmp_path / "direct/alice/source")


@pytest.mark.parametrize(
    "paths",
    [
        ["C:escape"], ["CON.txt"], ["trailing."], ["A", "a"], ["a", "a/b"], ["a\x00b"],
        ["caf\u00e9.txt", "cafe\u0301.txt"],
        ["caf\u00e9", "cafe\u0301/file.txt"],
    ],
)
def test_unsafe_paths(paths):
    with pytest.raises(TransferError):
        validate_paths([File(p, 0) for p in paths])


def test_symlink_destination_not_overwritten(tmp_path):
    with server() as (base, data):
        data["paths"] = ["README.md"]
        root = tmp_path / "repo"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.write_text("untouched")
        try:
            (root / "README.md").symlink_to(outside)
        except OSError:
            pytest.skip("symlinks unavailable")
        with pytest.raises(TransferError, match="symlink"):
            Download(options(root, base, force_download=True)).run()
        assert outside.read_text() == "untouched"


def test_cli_requires_destination_and_lists_preset():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["hf", "a/b"])
    with pytest.raises(ValueError):
        DownloadOptions("hf", "a/b", preset="author", layout="repo")
    args = parser.parse_args(["ms", "a/b", "file.pt", "--preset", "author"])
    assert args.filenames == ["file.pt"] and args.preset == "author"


def test_hf_network_retry_keeps_partial_bytes(tmp_path):
    with hf_server() as (base, data):
        data["drop_source"] = True
        opt = DownloadOptions(
            "hf",
            "alice/source",
            local_dir=tmp_path / "hf",
            endpoint=base,
            repo_type="model",
            filenames=("weights.bin",),
            progress="off",
            chunk_size=16384,
        )
        assert Download(opt).run()["downloaded"] == 1
        assert data["source_ranges"][0] is None
        assert data["source_ranges"][1].startswith("bytes=")
        assert data["source_ranges"][1] != "bytes=0-"
        assert (tmp_path / "hf/weights.bin").read_bytes() == BLOB


def test_dry_run_preserves_existing_partial(tmp_path):
    with server() as (base, data):
        root = tmp_path / "repo"
        opt = options(root, base, dry_run=True)
        state, partial = seed_partial(root, opt, 65536)
        before = {p.name: p.read_bytes() for p in state.directory.iterdir()}
        assert Download(opt).run()["files"] == 1
        assert {p.name: p.read_bytes() for p in state.directory.iterdir()} == before
        assert partial.stat().st_size == 65536
        assert not data["source_calls"]


def test_empty_file_and_existing_verified_file(tmp_path, monkeypatch):
    from hfms_bridge.hubs import ModelScope

    with server() as (base, data):
        root = tmp_path / "repo"
        monkeypatch.setattr(
            ModelScope, "_files", lambda *a: iter([File("empty", 0, sha(b""), REV)])
        )
        assert Download(options(root, base)).run()["downloaded"] == 1
        assert (root / "empty").read_bytes() == b""
        assert Download(options(root, base)).run()["skipped"] == 1
        assert not data["source_calls"]


def test_existing_directory_lock_excludes_second_writer(tmp_path):
    from filelock import FileLock, Timeout

    with server() as (base, data):
        root = tmp_path / "repo"
        state = DownloadState(root)
        state.prepare()
        with FileLock(str(state.directory / "task.lock")):
            with pytest.raises(Timeout):
                Download(options(root, base)).run()
        assert not data["source_calls"]


def test_sigint_keeps_partial_then_resumes(tmp_path, monkeypatch):
    import signal

    with server() as (base, data):
        data["paths"] = ["weights.pt"]
        root = tmp_path / "repo"
        opt = options(root, base, chunk_size=16384)
        download = Download(opt)
        original = download.progress.update
        fired = False

        def interrupt(size):
            nonlocal fired
            original(size)
            if size and not fired:
                fired = True
                handler = signal.getsignal(signal.SIGINT)
                handler(signal.SIGINT, None)

        monkeypatch.setattr(download.progress, "update", interrupt)
        with pytest.raises(KeyboardInterrupt):
            download.run()
        partial = DownloadState(root).paths("weights.pt")[1]
        assert 0 < partial.stat().st_size < len(BODY)
        assert Download(opt).run()["downloaded"] == 1
        assert (root / "weights.pt").read_bytes() == BODY


def test_cli_preset_layout_conflict():
    from hfms_bridge.download_cli import main

    with pytest.raises(SystemExit) as exc:
        main(["ms", "a/b", "--preset", "author", "--layout", "repo"])
    assert exc.value.code == 2
