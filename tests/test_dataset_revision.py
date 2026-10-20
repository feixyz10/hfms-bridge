import pytest

from hfms_bridge import Transfer
from hfms_bridge.http import HTTPError, TransferError
from hfms_bridge.hubs import ModelScope
from test_transfer import options, server


@pytest.mark.parametrize("tree", [{"Files": []}, {"files": []}, []])
def test_dataset_master_fallback(tree, monkeypatch):
    target = ModelScope("owner/repo", "dataset", "secret")
    calls = []

    def api(method, path, **kwargs):
        calls.append((path, kwargs))
        if path.endswith("/revisions"):
            raise HTTPError(404)
        return tree

    monkeypatch.setattr(target, "_api", api)
    try:
        target.check_revision()
        assert len(calls) == 2
        assert calls[1][0] == "datasets/owner/repo/repo/tree"
        assert calls[1][1]["params"]["Revision"] == "master"
        assert calls[1][1]["params"]["PageSize"] == 1
    finally:
        target.close()


@pytest.mark.parametrize(
    "repo_type,revision,status",
    [
        ("model", "master", 404),
        ("dataset", "master", 401),
        ("dataset", "master", 403),
        ("dataset", "master", 503),
        ("dataset", "release", 404),
    ],
)
def test_revision_fallback_is_narrow(repo_type, revision, status, monkeypatch):
    target = ModelScope("owner/repo", repo_type, "secret", revision=revision)
    calls = []

    def api(*args, **kwargs):
        calls.append(args)
        raise HTTPError(status)

    monkeypatch.setattr(target, "_api", api)
    try:
        with pytest.raises(TransferError):
            target.check_revision()
        assert len(calls) == 1
    finally:
        target.close()


@pytest.mark.parametrize("tree_status", [401, 403, 404, 503, None])
def test_revision_fallback_does_not_hide_tree_errors(tree_status, monkeypatch):
    target = ModelScope("owner/repo", "dataset", "secret")

    def api(method, path, **kwargs):
        if path.endswith("/revisions"):
            raise HTTPError(404)
        if tree_status:
            raise HTTPError(tree_status)
        return {"unrecognized": []}

    monkeypatch.setattr(target, "_api", api)
    try:
        with pytest.raises(TransferError):
            target.check_revision()
    finally:
        target.close()


def test_resume_dataset_without_revision_listing(tmp_path):
    with server() as (base, data):
        opt = options(tmp_path, base, repo_type="dataset")
        Transfer(opt, "ms-secret").run()
        before = (data["reads"], data["puts"], data["commits"])
        data["revisions_missing"] = True
        result = Transfer(opt, "ms-secret").run()
        assert result == {"committed": 0, "skipped": 2}
        assert (data["reads"], data["puts"], data["commits"]) == before


def test_ms_api_error_includes_safe_route(monkeypatch):
    from test_redirects import response

    target = ModelScope("owner/repo", "dataset", "secret")
    monkeypatch.setattr(
        target.http,
        "api_request",
        lambda *a, **k: response(
            "https://modelscope.cn/signed-secret-path?token=secret",
            status=404,
        ),
    )
    try:
        with pytest.raises(HTTPError) as exc:
            target._api("GET", "datasets/owner/repo/revisions")
        assert "GET modelscope.cn/api/v1/datasets/owner/repo/revisions" in str(exc.value)
        assert "secret" not in str(exc.value)
    finally:
        target.close()
