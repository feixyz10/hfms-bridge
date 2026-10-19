import hashlib
import io
from types import SimpleNamespace

import pytest
import requests

from hf2ms import Transfer
from hf2ms.http import (
    HTTP, HTTPError, CheckedStream, ResumingReader, RetryableTransferError,
    TransferError, error_stage,
)
from test_transfer import options, server


@pytest.mark.parametrize('workers', [1, 3])
@pytest.mark.parametrize('omit_hash', [False, True])
def test_source_disconnect_keeps_upload_and_progress(tmp_path, monkeypatch, caplog, workers, omit_hash):
    with server() as (base, data):
        data.update(drop_source=True, omit_hash=omit_hash)
        opt = options(tmp_path, base, workers=workers, chunk_size=16384)
        opt.attempts = 2
        monkeypatch.setattr(Transfer, '_backoff', lambda *args: None)
        Transfer(opt, 'ms-secret').run()
        assert data['blob']
        assert data['puts'] == 1
        ranges = data['source_ranges']
        assert ranges[0] is None
        assert ranges[1].startswith('bytes=')
        assert int(ranges[1][6:-1]) > 0
        assert 'resuming at byte' in caplog.text
        assert 'MS upload' not in caplog.text
        before = data['puts']
        Transfer(opt, 'ms-secret').run()
        assert data['puts'] == before


@pytest.mark.parametrize("failure", ["ignore_range", "encoded_range"])
def test_invalid_range_safely_restarts_file(tmp_path, monkeypatch, caplog, failure):
    with server() as (base, data):
        data.update(drop_source=True)
        data[failure] = True
        opt = options(tmp_path, base, chunk_size=16384)
        opt.attempts = 2
        monkeypatch.setattr(Transfer, '_backoff', lambda *args: None)
        Transfer(opt, 'ms-secret').run()
        assert data['blob']
        assert data['puts'] == 2
        assert data['source_ranges'][-1] is None
        assert 'HF read: HF Range response' in caplog.text


@pytest.mark.parametrize('status,content_range,length', [
    (200, 'bytes 3-5/6', '3'),
    (416, '', '0'),
    (206, 'bytes 2-5/6', '3'),
    (206, 'bytes 3-4/6', '2'),
    (206, 'bytes 3-5/7', '3'),
    (206, 'bytes 3-5/*', '3'),
    (206, '', '3'),
    (206, 'bytes 3-5/6', '6'),
])
def test_invalid_ranges_rejected_and_closed(monkeypatch, status, content_range, length):
    response = requests.Response()
    response.status_code = status
    response.headers.update({'Content-Range': content_range, 'Content-Length': length})
    closed = []
    response.close = lambda: closed.append(True)
    http = HTTP()
    monkeypatch.setattr(http, 'request', lambda *args, **kwargs: response)
    with pytest.raises(RetryableTransferError):
        http.download('https://huggingface.co/file', offset=3, total=6)
    assert closed
    http.close()


def test_range_survives_redirect_without_credentials(monkeypatch):
    seen = []
    first, second = requests.Response(), requests.Response()
    first.status_code = 302
    first.headers['Location'] = 'https://cdn.example.org/file?signature=secret'
    first._content = b''
    first._content_consumed = True
    second.status_code = 206
    second.headers.update({'Content-Range': 'bytes 3-5/6', 'Content-Length': '3'})
    second._content = b'def'
    responses = iter([first, second])
    http = HTTP()
    def request(*args, **kwargs):
        seen.append(dict(kwargs['headers']))
        return next(responses)
    monkeypatch.setattr(http, 'request', request)
    assert http.download('https://huggingface.co/file', 'secret', 3, 6) is second
    assert seen[0]['Authorization'] == 'Bearer secret'
    assert 'Authorization' not in seen[1]
    assert seen[1]['Range'] == 'bytes=3-'
    http.close()


def test_partial_read_exception_does_not_skip_bytes_or_double_count():
    body = b'abcdefghij'
    offsets, closed, progress = [], [], []
    class Broken(io.BytesIO):
        def read(self, n):
            if self.tell() == 3:
                super().read(2)  # consumed internally, never returned to caller
                raise requests.ConnectionError('signed-url-secret')
            return super().read(n)
    def opener(offset):
        offsets.append(offset)
        raw = Broken(body) if len(offsets) == 1 else io.BytesIO(body[offset:])
        return SimpleNamespace(raw=raw, close=lambda: closed.append(offset))
    with ResumingReader(opener, len(body), 2, lambda _: None, lambda: None,
                        lambda *args: None) as raw:
        checked = CheckedStream(raw, len(body), hashlib.sha256(body).hexdigest(), 3,
                                on_read=progress.append)
        result = b''
        while chunk := checked.read():
            result += chunk
        checked.verify()
    assert result == body
    assert offsets == [0, 3]
    assert sum(progress) == len(body)
    assert closed == [0, 3]


def test_retry_budget_and_cancellation():
    opened, closed = [], []
    def opener(offset):
        opened.append(offset)
        return SimpleNamespace(raw=io.BytesIO(b''), close=lambda: closed.append(offset))
    with ResumingReader(opener, 10, 3, lambda _: None, lambda: None,
                        lambda *args: None) as raw:
        with pytest.raises(RetryableTransferError, match='HF read.*exhausted'):
            raw.read(2)
    assert len(opened) == len(closed) == 3
    def cancel():
        raise TransferError('Transfer interrupted')
    with ResumingReader(opener, 10, 3, lambda _: None, lambda: None,
                        lambda *args: None) as raw:
        raw.cancelled = cancel
        with pytest.raises(TransferError, match='interrupted'):
            raw.read(2)
    assert len(opened) == len(closed) == 4


def test_stage_does_not_expose_tokens_or_mislabel_source():
    with pytest.raises(RetryableTransferError) as exc:
        with error_stage('MS upload'):
            with error_stage('HF read'):
                raise requests.ConnectionError('https://secret?token=secret')
    assert str(exc.value) == 'HF read: network ConnectionError'
    with pytest.raises(HTTPError) as exc:
        with error_stage('MS commit'):
            raise HTTPError(403)
    assert exc.value.status == 403
    assert str(exc.value).startswith('MS commit:')


@pytest.mark.parametrize('failure,stage', [('fail_put', 'MS upload'), ('fail_commit', 'MS commit')])
def test_destination_stage_in_retry_log(tmp_path, monkeypatch, caplog, failure, stage):
    with server() as (base, data):
        data[failure] = True
        opt = options(tmp_path, base)
        opt.attempts = 2
        monkeypatch.setattr(Transfer, '_backoff', lambda *args: None)
        with pytest.raises(HTTPError, match=stage):
            Transfer(opt, 'ms-secret').run()
        assert stage in caplog.text


@pytest.mark.parametrize("offset", [0, 3])
def test_encoding_rejection_is_retryable_only_during_recovery(monkeypatch, offset):
    response = requests.Response()
    response.status_code = 206 if offset else 200
    response.headers.update({
        "Content-Range": "bytes 3-5/6",
        "Content-Length": "3" if offset else "6",
        "Content-Encoding": "gzip",
    })
    closed = []
    response.close = lambda: closed.append(True)
    http = HTTP()
    monkeypatch.setattr(http, "request", lambda *args, **kwargs: response)
    try:
        with pytest.raises(TransferError, match="Accept-Encoding") as exc:
            http.download("https://huggingface.co/file", offset=offset, total=6)
        assert isinstance(exc.value, RetryableTransferError) == bool(offset)
        assert closed == [True]
    finally:
        http.close()
