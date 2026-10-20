import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from hfms_bridge import Transfer
from hfms_bridge.cli import build_parser, byte_size, main
from hfms_bridge.http import HTTPError, TransferError
from hfms_bridge.hubs import HuggingFace, ModelScope
from hfms_bridge.state import Cache, State
from test_transfer import BLOB, COMMIT, SHA, options, server


def test_cli_ms_token_required_and_hf_optional(monkeypatch, capsys):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.setenv("MODELSCOPE_API_TOKEN", "not-an-implicit-flag")
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["hf2ms", "a/b", "c/d"])
    assert exc.value.code == 2
    assert "--ms-token" in capsys.readouterr().err
    args = parser.parse_args(["hf2ms", "a/b", "c/d", "--ms-token", "test"])
    assert args.hf_token is None
    assert args.repo_type == "auto"
    assert args.ms_revision == "master"
    assert args.hf_revision == "main"
    assert args.on_conflict == "overwrite"
    assert args.cache_dir == Path.home() / ".cache" / "hfms-bridge"
    assert args.cache_max_size == 256 * 1024**2
    with pytest.raises(SystemExit):
        parser.parse_args(["hf2ms", "a/b", "c/d", "--ms-token", ""])


def test_cli_patterns_and_token_precedence(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "env-secret")
    args = build_parser().parse_args(
        [
            "hf2ms",
            "a/b",
            "c/d",
            "--ms-token",
            "test",
            "--hf-token",
            "explicit-secret",
            "--include",
            "*.json",
            "*.bin",
            "--include",
            "README.md",
            "--exclude",
            "tmp/*",
            "--cache-max-size",
            "32MiB",
            "--dry-run",
        ]
    )
    assert args.hf_token == "explicit-secret"
    assert args.include == ["*.json", "*.bin", "README.md"]
    assert args.cache_max_size == 32 * 1024**2
    assert byte_size("1KiB") == 1024


def test_cli_passes_tokens_separately(monkeypatch, capsys):
    captured = {}

    class FakeTransfer:
        def __init__(self, options, ms_token, hf_token):
            captured.update(options=options, ms_token=ms_token, hf_token=hf_token)

        def run(self):
            return {"dry_run": True, "files": 0}

    monkeypatch.setattr("hfms_bridge.cli.Transfer", FakeTransfer)
    assert (
        main(
            ["hf2ms", "a/b", "c/d", "--ms-token", "secret", "--visibility", "private", "--dry-run"]
        )
        == 0
    )
    assert captured["ms_token"] == "secret"
    assert captured["options"].visibility == "private"
    assert "secret" not in capsys.readouterr().out
    assert not hasattr(captured["options"], "ms_token")


@pytest.mark.parametrize("kind", ["model", "dataset"])
def test_auto_detect_and_create_private_repo(tmp_path, kind):
    with server() as (base, data):
        data.update(hf_types=(kind,), exists=False, hf_private=True)
        opt = options(tmp_path, base, repo_type="auto")
        Transfer(opt, "ms-secret").run()
        assert data["created"] == 1
        if kind == "model":
            body = json.loads(data["create_body"])
            assert body["Visibility"] == 1
            assert body["Path"] == "bob"
            assert body["License"] == "mit"
        else:
            raw = data["create_body"]
            assert b'name="Owner"' in raw
            assert b'name="Visibility"\r\n\r\n1\r\n' in raw
        assert data["puts"] == 1


def test_auto_ambiguity_does_not_write(tmp_path):
    with server() as (base, data):
        with pytest.raises(TransferError, match="Both model and dataset"):
            Transfer(options(tmp_path, base, repo_type="auto"), "ms-secret").run()
        assert data["created"] == data["commits"] == data["puts"] == 0
        assert not list(tmp_path.iterdir())


def test_auto_does_not_treat_permission_denial_as_absence(monkeypatch):
    source = HuggingFace("a/b", "auto")
    try:

        def denied(*args, **kwargs):
            raise HTTPError(403)

        monkeypatch.setattr(source, "_get", denied)
        with pytest.raises(HTTPError):
            source.detect()
    finally:
        source.close()


def test_missing_branch_fails_before_upload(tmp_path):
    with server() as (base, data):
        with pytest.raises(TransferError, match="MS branch"):
            Transfer(options(tmp_path, base, ms_revision="release"), "ms-secret").run()
        assert data["commits"] == data["puts"] == 0
        assert not list(tmp_path.iterdir())


def test_conflict_error_preflights_before_any_commit(tmp_path):
    with server() as (base, data):
        # The first file is new; the second conflicts. Nothing may be uploaded.
        data["remote"]["README.md"] = {"Size": 6, "Sha256": "f" * 64}
        with pytest.raises(TransferError, match="no remote writes"):
            Transfer(options(tmp_path, base, on_conflict="error"), "ms-secret").run()
        assert data["puts"] == data["commits"] == 0


def test_overwrite_and_skip_policies(tmp_path):
    for policy in ("skip", "overwrite"):
        with server() as (base, data):
            data["remote"]["weights.bin"] = {"Size": len(BLOB), "Sha256": "f" * 64}
            result = Transfer(
                options(tmp_path / policy, base, on_conflict=policy), "ms-secret"
            ).run()
            assert data["puts"] == (0 if policy == "skip" else 1)
            assert result["skipped"] == (1 if policy == "skip" else 0)


def test_dry_run_no_payload_no_writes_no_cache(tmp_path):
    with server() as (base, data):
        data["exists"] = False
        cache_dir = tmp_path / "must-not-exist"
        result = Transfer(options(cache_dir, base, dry_run=True), "ms-secret").run()
        assert result["create_repo"] is True
        assert result["files"] == 2
        assert result["bytes"] == len(BLOB) + 5
        assert data["puts"] == data["commits"] == data["created"] == data["reads"] == 0
        assert not cache_dir.exists()


def test_dry_run_preserves_existing_cache_and_uses_pinned_revision(tmp_path):
    with server() as (base, data):
        data["fail_commit"] = True
        opt = options(tmp_path, base)
        with pytest.raises(HTTPError):
            Transfer(opt, "ms-secret").run()
        before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in tmp_path.iterdir()}
        data["source_commit"] = "b" * 40
        reads = data["reads"]
        opt.dry_run = True
        result = Transfer(opt, "ms-secret").run()
        assert result["source_commit"] == COMMIT
        assert data["reads"] == reads
        assert before == {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in tmp_path.iterdir()}


def test_completed_task_resolves_new_source_commit(tmp_path):
    with server() as (base, data):
        opt = options(tmp_path, base)
        Transfer(opt, "ms-secret").run()
        data["source_commit"] = "b" * 40
        Transfer(opt, "ms-secret").run()
        with State(next(tmp_path.glob("*.sqlite3"))) as state:
            assert state.get("source_commit") == "b" * 40
            assert state.get("complete") is True
        assert data["puts"] == 1


def test_remote_changed_after_success_is_repaired(tmp_path):
    with server() as (base, data):
        opt = options(tmp_path, base)
        Transfer(opt, "ms-secret").run()
        data["remote"]["README.md"] = {"Size": 1, "Sha256": "e" * 64}
        result = Transfer(opt, "ms-secret").run()
        assert result == {"committed": 1, "skipped": 1}
        assert data["remote"]["README.md"]["Sha256"] == hashlib.sha256(b"hello").hexdigest()


def test_dry_run_unknown_hash_is_not_identical(tmp_path):
    with server() as (base, data):
        data["remote"]["README.md"] = {"Size": 5, "Sha256": hashlib.sha256(b"hello").hexdigest()}
        report = Transfer(options(tmp_path, base, dry_run=True), "ms-secret").run()
        assert report["unknown"] == 1
        assert data["reads"] == 0


def make_job(cache, name, complete):
    identity = {"name": name}
    with cache.open(identity) as state:
        state.set("complete", complete)
        state.mark("file", 1, SHA, "committed")
    return cache.path(identity)


def test_cache_lru_only_evicts_completed_jobs(tmp_path):
    cache = Cache(tmp_path, 1024**2)
    old = make_job(cache, "old", True)
    newer = make_job(cache, "newer", True)
    unfinished = make_job(cache, "unfinished", False)
    os.utime(old, (time.time() - 100, time.time() - 100))
    total = sum(p.stat().st_size for p in tmp_path.glob("*.sqlite3"))
    cache.max_size = total - old.stat().st_size
    cache.enforce(unfinished)
    assert not old.exists()
    assert newer.exists() and unfinished.exists()


def test_cache_keeps_active_completed_job(tmp_path):
    cache = Cache(tmp_path, 1024**2)
    active = make_job(cache, "active", True)
    with State(active):
        cache.max_size = 1
        with pytest.raises(TransferError, match="unfinished checkpoints"):
            cache.enforce(tmp_path / "different")
        assert active.exists()


def test_cache_budget_preserves_unfinished_checkpoint(tmp_path):
    cache = Cache(tmp_path, 1024**2)
    pending = make_job(cache, "pending", False)
    cache.max_size = 1
    with pytest.raises(TransferError, match="Cache limit"):
        cache.enforce(tmp_path / "different")
    with sqlite3.connect(pending) as db:
        assert db.execute("SELECT count(*) FROM files").fetchone()[0] == 1


def test_truncated_model_listing_is_not_missing_file(monkeypatch):
    target = ModelScope("a/b", "model", "secret")
    try:
        monkeypatch.setattr(target, "_api", lambda *a, **k: {"Files": [{}] * 3000})
        with pytest.raises(TransferError, match="truncated"):
            target.file("missing.bin")
    finally:
        target.close()
