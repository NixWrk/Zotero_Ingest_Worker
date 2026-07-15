from __future__ import annotations

import socket
import urllib.error
import urllib.request
from typing import Any

import pytest

from zotero_metadata_enrichment import safe_http


_PUBLIC_RECORD = (
    socket.AF_INET,
    socket.SOCK_STREAM,
    socket.IPPROTO_TCP,
    "",
    ("93.184.216.34", 443),
)


class FakeResponse:
    def __init__(self, status: int, *, location: str = "", body: bytes = b"ok") -> None:
        self.status = status
        self.code = status
        self.reason = "test"
        self.headers = {"Location": location} if location else {}
        self.body = body
        self.closed = False

    def read(self, _amount: int | None = None) -> bytes:
        return self.body

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _public_dns(_host: str, port: int, **_kwargs: Any) -> list[tuple[Any, ...]]:
    return [(*_PUBLIC_RECORD[:4], ("93.184.216.34", port))]


def test_private_initial_target_is_blocked_before_open(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        safe_http.socket,
        "getaddrinfo",
        lambda _host, port, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))
        ],
    )
    monkeypatch.setattr(
        safe_http,
        "_open_pinned_once",
        lambda request, **_kwargs: calls.append(request.full_url),
    )

    with pytest.raises(safe_http.UnsafeUrlError, match="Blocked resolved address"):
        safe_http.safe_urlopen("https://example.org/a", timeout=1)

    assert calls == []


def test_dns_failure_remains_retryable_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = socket.gaierror(socket.EAI_AGAIN, "temporary DNS failure")
    monkeypatch.setattr(
        safe_http.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(socket.gaierror) as caught:
        safe_http.safe_urlopen("https://source.example/start", timeout=1)

    assert caught.value is failure
    assert not isinstance(caught.value, safe_http.UnsafeUrlError)


def test_private_redirect_is_blocked_before_second_open(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    response = FakeResponse(302, location="https://private.example/secret")

    def fake_dns(host: str, port: int, **_kwargs: Any) -> list[tuple[Any, ...]]:
        address = "127.0.0.1" if host == "private.example" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]

    def fake_open(request: urllib.request.Request, **_kwargs: Any) -> FakeResponse:
        calls.append(request.full_url)
        return response

    monkeypatch.setattr(safe_http.socket, "getaddrinfo", fake_dns)
    monkeypatch.setattr(safe_http, "_open_pinned_once", fake_open)

    with pytest.raises(safe_http.UnsafeUrlError, match="Blocked resolved address"):
        safe_http.safe_urlopen("https://public.example/a", timeout=1)

    assert calls == ["https://public.example/a"]
    assert response.closed is True


def test_mixed_public_and_private_dns_is_rejected_before_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        safe_http.socket,
        "getaddrinfo",
        lambda _host, port, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.7", port)),
        ],
    )
    monkeypatch.setattr(
        safe_http,
        "_open_pinned_once",
        lambda *_args, **_kwargs: pytest.fail("mixed DNS answer must fail closed"),
    )

    with pytest.raises(safe_http.UnsafeUrlError, match="Blocked resolved address"):
        safe_http.safe_urlopen("https://mixed.example/a", timeout=1)


def test_cross_host_policy_blocks_before_second_open(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(safe_http.socket, "getaddrinfo", _public_dns)

    def fake_open(request: urllib.request.Request, **_kwargs: Any) -> FakeResponse:
        calls.append(request.full_url)
        return FakeResponse(302, location="https://other.example/asset")

    monkeypatch.setattr(safe_http, "_open_pinned_once", fake_open)

    with pytest.raises(safe_http.UnsafeUrlError, match="Redirect policy rejected"):
        safe_http.safe_urlopen(
            "https://source.example/asset",
            timeout=1,
            redirect_validator=safe_http.same_host_redirect,
        )

    assert calls == ["https://source.example/asset"]


def test_allowed_public_redirect_opens_each_validated_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    before_open: list[str] = []
    monkeypatch.setattr(safe_http.socket, "getaddrinfo", _public_dns)

    def fake_open(request: urllib.request.Request, **_kwargs: Any) -> FakeResponse:
        calls.append(request.full_url)
        if len(calls) == 1:
            return FakeResponse(302, location="/final")
        return FakeResponse(200, body=b"done")

    monkeypatch.setattr(safe_http, "_open_pinned_once", fake_open)

    with safe_http.safe_urlopen(
        "https://source.example/start",
        timeout=1,
        before_open=before_open.append,
    ) as response:
        assert response.read() == b"done"

    assert calls == ["https://source.example/start", "https://source.example/final"]
    assert before_open == calls


def test_https_downgrade_is_blocked_before_second_open(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(safe_http.socket, "getaddrinfo", _public_dns)

    def fake_open(request: urllib.request.Request, **_kwargs: Any) -> FakeResponse:
        calls.append(request.full_url)
        return FakeResponse(302, location="http://source.example/plain")

    monkeypatch.setattr(safe_http, "_open_pinned_once", fake_open)

    with pytest.raises(safe_http.UnsafeUrlError, match="HTTPS downgrade"):
        safe_http.safe_urlopen("https://source.example/start", timeout=1)

    assert calls == ["https://source.example/start"]


def test_invalid_redirect_scheme_is_classified_and_response_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = FakeResponse(302, location="file:///etc/passwd")
    monkeypatch.setattr(safe_http.socket, "getaddrinfo", _public_dns)
    monkeypatch.setattr(
        safe_http,
        "_open_pinned_once",
        lambda _request, **_kwargs: response,
    )

    with pytest.raises(safe_http.UnsafeUrlError) as caught:
        safe_http.safe_urlopen("https://source.example/start", timeout=1)

    assert caught.value.is_redirect is True
    assert caught.value.target_url == "file:///etc/passwd"
    assert response.closed is True


def test_redirect_limit_error_is_classified_as_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = FakeResponse(302, location="/next")
    monkeypatch.setattr(safe_http.socket, "getaddrinfo", _public_dns)
    monkeypatch.setattr(
        safe_http,
        "_open_pinned_once",
        lambda _request, **_kwargs: response,
    )

    with pytest.raises(safe_http.UnsafeUrlError) as caught:
        safe_http.safe_urlopen(
            "https://source.example/start",
            timeout=1,
            max_redirects=0,
        )

    assert caught.value.is_redirect is True
    assert caught.value.target_url == "https://source.example/next"
    assert response.closed is True


def test_http_error_keeps_bounded_body_readable_for_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = FakeResponse(429, body=b'{"retry":true}')
    monkeypatch.setattr(safe_http.socket, "getaddrinfo", _public_dns)
    monkeypatch.setattr(
        safe_http,
        "_open_pinned_once",
        lambda _request, **_kwargs: response,
    )

    with pytest.raises(urllib.error.HTTPError) as caught:
        safe_http.safe_urlopen("https://source.example/start", timeout=1)

    assert caught.value.read(100) == b'{"retry":true}'
    caught.value.close()
    assert response.closed is True


@pytest.mark.parametrize(
    "url",
    [
        "http://user:password@example.org/",
        "http://example.org/path with space",
        "http://example.org/path\\segment",
        "http://example.org/path\r\nX-Test: injected",
    ],
)
def test_ambiguous_or_credentialed_urls_are_rejected_before_dns(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
) -> None:
    monkeypatch.setattr(
        safe_http.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: pytest.fail("invalid URL must fail before DNS"),
    )

    with pytest.raises(safe_http.UnsafeUrlError):
        safe_http.safe_urlopen(url, timeout=1)


@pytest.mark.parametrize("host", ["2130706433", "0177.0.0.1", "0x7f000001"])
def test_alternate_loopback_notation_is_blocked_after_resolution(
    monkeypatch: pytest.MonkeyPatch,
    host: str,
) -> None:
    monkeypatch.setattr(
        safe_http.socket,
        "getaddrinfo",
        lambda _host, port, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))
        ],
    )

    with pytest.raises(safe_http.UnsafeUrlError, match="Blocked resolved address"):
        safe_http.safe_urlopen(f"http://{host}/", timeout=1)


def test_trusted_loopback_must_be_explicitly_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        safe_http.socket,
        "getaddrinfo",
        lambda _host, port, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))
        ],
    )
    monkeypatch.setattr(
        safe_http,
        "_open_pinned_once",
        lambda _request, **_kwargs: FakeResponse(200),
    )

    with pytest.raises(safe_http.UnsafeUrlError):
        safe_http.safe_urlopen("http://localhost:1969/", timeout=1)
    with safe_http.safe_urlopen(
        "http://localhost:1969/",
        timeout=1,
        allow_loopback=True,
        max_redirects=0,
    ) as response:
        assert response.read() == b"ok"


def test_cross_origin_redirect_strips_sensitive_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[urllib.request.Request] = []
    monkeypatch.setattr(safe_http.socket, "getaddrinfo", _public_dns)

    def fake_open(request: urllib.request.Request, **_kwargs: Any) -> FakeResponse:
        requests.append(request)
        if len(requests) == 1:
            return FakeResponse(302, location="https://other.example/final")
        return FakeResponse(200)

    monkeypatch.setattr(safe_http, "_open_pinned_once", fake_open)
    request = urllib.request.Request(
        "https://source.example/start",
        headers={
            "Authorization": "Bearer secret",
            "X-API-Key": "secret",
            "X-Trace": "keep",
        },
    )

    with safe_http.safe_urlopen(request, timeout=1):
        pass

    redirected_headers = {
        name.casefold(): value for name, value in requests[1].header_items()
    }
    assert "authorization" not in redirected_headers
    assert "x-api-key" not in redirected_headers
    assert redirected_headers["x-trace"] == "keep"


def test_post_redirect_to_get_drops_body_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[urllib.request.Request] = []
    monkeypatch.setattr(safe_http.socket, "getaddrinfo", _public_dns)

    def fake_open(request: urllib.request.Request, **_kwargs: Any) -> FakeResponse:
        requests.append(request)
        if len(requests) == 1:
            return FakeResponse(302, location="/final")
        return FakeResponse(200)

    monkeypatch.setattr(safe_http, "_open_pinned_once", fake_open)
    request = urllib.request.Request(
        "https://source.example/start",
        data=b"payload",
        headers={"Content-Type": "application/json", "Transfer-Encoding": "chunked"},
        method="POST",
    )

    with safe_http.safe_urlopen(request, timeout=1):
        pass

    redirected = requests[1]
    headers = {name.casefold(): value for name, value in redirected.header_items()}
    assert redirected.get_method() == "GET"
    assert redirected.data is None
    assert not ({"content-length", "content-type", "transfer-encoding"} & headers.keys())


def test_reused_request_is_resolved_fresh_without_mutating_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dns_calls: list[str] = []

    def fake_dns(host: str, port: int, **_kwargs: Any) -> list[tuple[Any, ...]]:
        dns_calls.append(host)
        return [(*_PUBLIC_RECORD[:4], ("93.184.216.34", port))]

    monkeypatch.setattr(safe_http.socket, "getaddrinfo", fake_dns)
    monkeypatch.setattr(
        safe_http,
        "_open_pinned_once",
        lambda _request, **_kwargs: FakeResponse(200),
    )
    request = urllib.request.Request("https://source.example/start")

    with safe_http.safe_urlopen(request, timeout=1):
        pass
    with safe_http.safe_urlopen(request, timeout=1):
        pass

    assert dns_calls == ["source.example", "source.example"]
    assert not hasattr(request, "_zotero_resolved_addresses")


def test_pinned_connection_dials_resolved_sockaddr_without_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    address = safe_http.ResolvedAddress(
        family=socket.AF_INET,
        socktype=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
        sockaddr=("93.184.216.34", 443),
        ip="93.184.216.34",
    )
    events: list[tuple[str, object]] = []

    class FakeSocket:
        def settimeout(self, value: float) -> None:
            events.append(("timeout", value))

        def connect(self, sockaddr: tuple[str, int]) -> None:
            events.append(("connect", sockaddr))

        def getpeername(self) -> tuple[str, int]:
            return ("93.184.216.34", 443)

        def close(self) -> None:
            events.append(("close", None))

    monkeypatch.setattr(safe_http.socket, "socket", lambda *_args: FakeSocket())
    monkeypatch.setattr(
        safe_http.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: pytest.fail("pinned dial must not resolve DNS again"),
    )

    sock = safe_http._dial_resolved_address(address, 3.0)

    assert isinstance(sock, FakeSocket)
    assert events == [("timeout", 3.0), ("connect", ("93.184.216.34", 443))]
