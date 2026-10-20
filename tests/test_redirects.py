import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from hfms_bridge.http import HTTP, HTTPError, TransferError, payload, trusted_api_redirect
from hfms_bridge.hubs import HuggingFace, ModelScope


@contextmanager
def redirect_server():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_api(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            calls.append((self.command, self.path, self.headers.get("Authorization"), body))
            if not self.path.startswith("/canonical/"):
                self.send_response(308)
                self.send_header("Location", "/canonical" + self.path)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if "/api/models/" in self.path:
                status, result = 401, {}
            elif "/api/datasets/" in self.path:
                status, result = 200, {"private": False}
            else:
                status, result = 200, {"Data": {"ok": True}}
            output = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(output)))
            self.end_headers()
            self.wfile.write(output)

        do_GET = handle_api
        do_POST = handle_api

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", calls
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def test_hf_auto_308_then_401_still_finds_dataset():
    with redirect_server() as (base, calls):
        source = HuggingFace(
            "tasksource/tasksource-jev-typed-decisions", "auto", token="hf-secret", endpoint=base
        )
        try:
            assert source.detect() == "dataset"
            assert len(calls) == 4
            assert all(c[2] == "Bearer hf-secret" for c in calls)
        finally:
            source.close()


@pytest.mark.parametrize("repo_type", ["model", "dataset"])
def test_ms_api_308_preserves_get_and_replayable_post(repo_type):
    with redirect_server() as (base, calls):
        target = ModelScope("user/repo", repo_type, "ms-secret", endpoint=base)
        try:
            assert target.repo_exists()
            target.create_repo("private", "mit")
            assert [c[0] for c in calls] == ["GET", "GET", "POST", "POST"]
            assert all(c[2] == "Bearer ms-secret" for c in calls)
            if repo_type == "model":
                assert json.loads(calls[-2][3]) == json.loads(calls[-1][3])
            else:
                assert b'name="Owner"\r\n\r\nuser\r\n' in calls[-1][3]
        finally:
            target.close()


def test_redirect_query_params_not_duplicated():
    with redirect_server() as (base, calls):
        http = HTTP()
        try:
            with http.api_request("GET", base + "/files", params={"Revision": "main"}) as response:
                payload(response)
            assert calls[-1][1] == "/canonical/files?Revision=main"
        finally:
            http.close()


def response(url, status=308, location=None):
    result = requests.Response()
    result.status_code = status
    result.url = url
    result._content = b"{}"
    result._content_consumed = True
    result.request = requests.Request("GET", url).prepare()
    if location is not None:
        result.headers["Location"] = location
    return result


def test_official_alias_preserves_auth_and_body(monkeypatch):
    http = HTTP()
    calls = []

    def send(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if len(calls) == 1:
            return response(url, location="https://www.modelscope.cn/api/v1/models")
        return response(url, status=200)

    monkeypatch.setattr(http, "request", send)
    try:
        with http.api_request(
            "POST",
            "https://modelscope.cn/api/v1/models",
            json={"Name": "repo"},
            headers={"Authorization": "Bearer secret"},
        ):
            pass
        assert calls[1][0] == "POST"
        assert calls[1][2]["json"] == {"Name": "repo"}
        assert calls[1][2]["headers"]["Authorization"] == "Bearer secret"
    finally:
        http.close()


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/collect",
        "https://modelscope.cn.evil.example/collect",
        "https://arbitrary.modelscope.cn/collect",
        "http://modelscope.cn/collect",
        "https://www.modelscope.cn:8443/collect",
    ],
)
def test_untrusted_redirect_not_requested(monkeypatch, target):
    http = HTTP()
    calls = []

    def send(method, url, **kwargs):
        calls.append(url)
        return response(url, location=target)

    monkeypatch.setattr(http, "request", send)
    try:
        with pytest.raises((TransferError, ValueError)):
            http.api_request(
                "GET", "https://modelscope.cn/start", headers={"Authorization": "Bearer secret"}
            )
        assert calls == ["https://modelscope.cn/start"]
    finally:
        http.close()


@pytest.mark.parametrize(
    "status,location,method,error",
    [
        (308, "/loop", "GET", "loop"),
        (308, None, "GET", "Location"),
        (302, "/next", "POST", "request method"),
    ],
)
def test_invalid_redirects(monkeypatch, status, location, method, error):
    http = HTTP()
    monkeypatch.setattr(http, "request", lambda m, u, **k: response(u, status, location))
    try:
        with pytest.raises(TransferError, match=error):
            http.api_request(method, "https://modelscope.cn/loop", json={})
    finally:
        http.close()


def test_no_stream_replay_in_api_helper():
    import io

    http = HTTP()
    try:
        with pytest.raises(ValueError, match="streaming"):
            http.api_request("PUT", "https://modelscope.cn/upload", data=io.BytesIO(b"data"))
    finally:
        http.close()


def test_error_context_does_not_leak_signed_query():
    result = response("https://example.org/path?token=secret", status=403)
    with pytest.raises(HTTPError) as exc:
        payload(result)
    assert "GET example.org" in str(exc.value)
    assert "secret" not in str(exc.value)


def test_trusted_origin_normalizes_default_port():
    assert trusted_api_redirect("https://modelscope.cn/path", "https://modelscope.cn:443/next")


def test_hf_mirror_to_official_is_one_way():
    assert trusted_api_redirect(
        "https://hf-mirror.com/api/models/a/b", "https://huggingface.co/api/models/a/b"
    )
    assert not trusted_api_redirect(
        "https://huggingface.co/api/models/a/b", "https://hf-mirror.com/api/models/a/b"
    )
