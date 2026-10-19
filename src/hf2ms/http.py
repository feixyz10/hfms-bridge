"""Explicit HTTP error handling and bounded, integrity-checked request bodies."""

import hashlib
import re
from contextlib import contextmanager
from urllib.parse import urljoin, urlparse

import requests
from urllib3.exceptions import HTTPError as UrllibError


class TransferError(Exception):
    pass


class RetryableTransferError(TransferError):
    """A response that can be retried without trusting incomplete metadata."""


class HTTPError(TransferError):
    def __init__(self, status, retry_after=None, context=None):
        # Do not put signed URLs, tokens, or server-echoed bodies in logs.
        label = "HTTP request" if not context else "HTTP " + context
        super().__init__(f"{label} failed (status {status})")
        self.status = status
        self.retry_after = retry_after


@contextmanager
def error_stage(stage):
    """Label failures without exposing request URLs, headers or exception bodies."""
    try:
        yield
    except TransferError as exc:
        if not getattr(exc, "stage", None):
            exc.stage = stage
            exc.args = (f"{stage}: {exc}",)
        raise
    except (requests.RequestException, UrllibError) as exc:
        failure = RetryableTransferError(f"{stage}: network {type(exc).__name__}")
        failure.stage = stage
        raise failure from exc


def check(response):
    if not 200 <= response.status_code < 300:
        raise HTTPError(
            response.status_code, response.headers.get("Retry-After"), response_context(response)
        )


def response_context(response):
    method = getattr(getattr(response, "request", None), "method", "request")
    return f"{method} {urlparse(response.url or '').hostname or 'unknown host'}"


def payload(response):
    check(response)
    body = response.json()
    if isinstance(body, dict):
        if str(body.get("Code")) in ("401", "403", "404", "409", "429", "500", "502", "503", "504"):
            raise HTTPError(
                int(body["Code"]), response.headers.get("Retry-After"), response_context(response)
            )
        if body.get("Success") is False or body.get("success") is False:
            raise TransferError("API rejected the operation")
        if body.get("Code") not in (None, 0, 200, "200"):
            raise TransferError("API returned an unsuccessful business status")
        return body.get("Data", body)
    return body


def origin(url):
    p = urlparse(url)
    return p.scheme, p.hostname, p.port or (443 if p.scheme == "https" else 80)


def trusted_api_redirect(source, destination):
    """Permit same-origin and explicit official aliases, never arbitrary subdomains."""
    validate_url(destination)
    if origin(source) == origin(destination):
        return True
    left, right = origin(source), origin(destination)
    if left[0] != "https" or right[0] != "https" or left[2] != 443 or right[2] != 443:
        return False
    # The mirror may hand API requests back to official HF. Never allow the
    # reverse direction to forward official-HF credentials to a third party.
    if left[1] == "hf-mirror.com" and right[1] in ("huggingface.co", "www.huggingface.co"):
        return True
    aliases = (
        {"modelscope.cn", "www.modelscope.cn"},
        {"modelscope.ai", "www.modelscope.ai"},
        {"huggingface.co", "www.huggingface.co", "hf.co"},
    )
    return any(left[1] in hosts and right[1] in hosts for hosts in aliases)


def validate_url(url):
    p = urlparse(url)
    if p.username or p.password or not p.hostname or p.fragment:
        raise ValueError("Invalid endpoint or transfer URL")
    if p.scheme != "https" and not (
        p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1", "::1")
    ):
        raise ValueError("HTTPS is required except for local test servers")
    return url.rstrip("/")


class HTTP:
    def __init__(self, timeout=120):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "hf2ms/0.3.2"
        self.timeout = (15, timeout)

    def close(self):
        self.session.close()

    def request(self, method, url, **kwargs):
        validate_url(url)
        # No automatic retries: consumed streaming bodies cannot be replayed.
        return self.session.request(method, url, timeout=self.timeout, **kwargs)

    def api_request(self, method, url, **kwargs):
        """Follow API redirects for replayable metadata requests only.

        Streamed PUT bodies deliberately do not use this method: replaying a
        consumed stream could silently upload truncated content.
        """
        if "data" in kwargs:
            raise ValueError("api_request does not accept streaming request bodies")
        for value in (kwargs.get("files") or {}).values():
            if not (
                isinstance(value, tuple)
                and len(value) == 2
                and value[0] is None
                and isinstance(value[1], str)
            ):
                raise ValueError("Only replayable text form fields are allowed")
        kwargs.pop("allow_redirects", None)
        visited = set()
        for _ in range(10):
            if url in visited:
                raise TransferError("API redirect loop detected")
            visited.add(url)
            response = self.request(method, url, allow_redirects=False, **kwargs)
            if response.status_code not in (301, 302, 303, 307, 308):
                return response
            status = response.status_code
            location = response.headers.get("Location")
            actual_url = response.url or url
            response.close()
            if not location:
                raise TransferError("API redirect omitted Location")
            if method.upper() not in ("GET", "HEAD") and status not in (307, 308):
                raise TransferError("Refusing API redirect that could change the request method")
            target = urljoin(actual_url, location)
            if not trusted_api_redirect(actual_url, target):
                raise TransferError(
                    "Refusing API redirect to an untrusted origin; "
                    "check --hf-endpoint / --ms-endpoint"
                )
            url = target
            # Location is the complete redirect target; never append old query params twice.
            kwargs.pop("params", None)
        raise TransferError("Too many API redirects")

    def download(self, url, token=None, offset=0, total=None):
        headers = {"Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        if token:
            headers["Authorization"] = "Bearer " + token
        # Handle redirects ourselves so cookies and auth cannot cross origins.
        for _ in range(10):
            response = self.request("GET", url, headers=headers, stream=True, allow_redirects=False)
            if response.status_code in (301, 302, 303, 307, 308):
                next_url = urljoin(url, response.headers.get("Location", ""))
                response.close()
                if origin(next_url) != origin(url):
                    headers.pop("Authorization", None)
                    self.session.cookies.clear()
                url = next_url
                continue
            try:
                if offset and response.status_code == 416:
                    raise RetryableTransferError("HF Range response rejected the requested offset")
                check(response)
                if offset:
                    match = re.fullmatch(
                        r"bytes (\d+)-(\d+)/(\d+)",
                        response.headers.get("Content-Range", ""),
                    )
                    if (response.status_code != 206 or not match or
                            tuple(map(int, match.groups())) != (offset, total - 1, total)):
                        raise RetryableTransferError("HF Range response has invalid status or range")
                    length = response.headers.get("Content-Length")
                    if length is not None and length != str(total - offset):
                        raise RetryableTransferError("HF Range response has invalid length")
                elif response.status_code != 200:
                    raise TransferError("Expected a full-file HTTP 200 response")
                if response.headers.get("Content-Encoding", "identity") != "identity":
                    if offset:
                        raise RetryableTransferError("HF Range response ignored Accept-Encoding: identity")
                    raise TransferError("Server ignored Accept-Encoding: identity")
                return response
            except BaseException:
                response.close()
                raise
        raise TransferError("Too many source redirects")


class CheckedStream:
    """Non-seekable body with a known length; read() is always bounded.

    Check the digest BEFORE returning the last bytes to the uploader. This
    prevents a corrupt source from completing an upload under the expected OID.
    """

    def __init__(self, raw, size, sha256, chunk_size=1024 * 1024, on_read=None):
        self.raw, self.size, self.expected = raw, size, sha256
        self.on_read = on_read
        self.chunk_size = chunk_size
        self.count = 0
        self.hash = hashlib.sha256()

    def __len__(self):
        return self.size

    @error_stage("HF read")
    def read(self, size=-1):
        remaining = self.size - self.count
        if not remaining:
            return b""
        amount = min(remaining, self.chunk_size, size if size >= 0 else self.chunk_size)
        if amount == 0:
            return b""
        data = self._read_raw(amount)
        if not data:
            raise TransferError("Source ended before its declared size")
        if len(data) > amount:
            raise TransferError("Source exceeded requested read size")
        self.count += len(data)
        self.hash.update(data)
        if self.count == self.size:
            if self._read_raw(1):
                raise TransferError("Source exceeded its declared size")
            self.verify()
        if self.on_read is not None:
            self.on_read(len(data))
        return data

    def _read_raw(self, size):
        try:
            return self.raw.read(size)
        except UrllibError as exc:
            raise requests.ConnectionError("Source connection interrupted") from exc

    def verify(self):
        if self.count != self.size:
            raise TransferError("Upload did not consume the complete source")
        if self.expected and self.hash.hexdigest() != self.expected:
            raise TransferError("Source SHA-256 mismatch")


class ResumingReader:
    """Reconnect the pinned source while keeping the destination PUT alive.

    Only returned bytes count: a raw.read() that raises may have consumed partial
    bytes internally, but those bytes must be fetched again. No payload is saved.
    The retry budget covers the entire reader, not each successful chunk.
    """

    def __init__(self, opener, size, attempts, backoff, cancelled, on_resume):
        self.opener, self.size = opener, size
        self.attempts, self.backoff = attempts, backoff
        self.cancelled, self.on_resume = cancelled, on_resume
        self.response = None
        self.offset = 0
        self.reconnects = 0

    def __enter__(self):
        with error_stage("HF read"):
            self.cancelled()
            self.response = self.opener(0)
        return self

    def __exit__(self, *args):
        if self.response is not None:
            self.response.close()

    def read(self, size):
        with error_stage("HF read"):
            while True:
                self.cancelled()
                try:
                    if self.response is None:
                        self.response = self.opener(self.offset)
                    data = self.response.raw.read(size)
                    if not data and self.offset < self.size:
                        raise requests.ConnectionError("Source ended early")
                    self.offset += len(data)
                    return data
                except (requests.RequestException, UrllibError) as exc:
                    if self.response is not None:
                        self.response.close()
                        self.response = None
                    # At EOF no remaining range exists; do not fabricate bytes.
                    if self.reconnects >= self.attempts - 1 or self.offset >= self.size:
                        raise RetryableTransferError(
                            f"HF read recovery exhausted ({type(exc).__name__}); "
                            "restarting file if attempts remain"
                        ) from exc
                    self.reconnects += 1
                    self.on_resume(self.offset, self.reconnects)
                    self.backoff(min(2 ** (self.reconnects - 1), 30))
