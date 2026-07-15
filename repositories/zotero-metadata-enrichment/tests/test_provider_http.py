from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from zotero_metadata_enrichment import provider_http
from zotero_metadata_enrichment.provider_http import HostThrottle, parse_retry_after_seconds, register_retry_after_from_http_error
from zotero_metadata_enrichment.providers.zotero_translation_server import TranslationServerClient


def test_host_throttle_waits_between_fast_same_host_requests() -> None:
    now = 100.0
    sleeps: list[float] = []

    def clock() -> float:
        return now

    def sleeper(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    throttle = HostThrottle(clock=clock, sleeper=sleeper)

    throttle.wait("api.crossref.org", min_interval_seconds=0.2)
    throttle.wait("api.crossref.org", min_interval_seconds=0.2)

    assert sleeps == [pytest.approx(0.2)]


def test_retry_after_http_error_sets_host_cooldown(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    now = 0.0
    sleeps: list[float] = []

    def clock() -> float:
        return now

    def sleeper(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    throttle = HostThrottle(clock=clock, sleeper=sleeper)
    monkeypatch.setattr(provider_http, "_GLOBAL_HOST_THROTTLE", throttle)
    exc = urllib.error.HTTPError(
        "https://api.crossref.org/works/10.1000/example",
        429,
        "Too Many Requests",
        {"Retry-After": "7"},
        None,
    )

    assert register_retry_after_from_http_error(exc) == 7.0

    throttle.wait("api.crossref.org", min_interval_seconds=0.0)

    assert sleeps == [7.0]


def test_parse_retry_after_seconds_accepts_delta_seconds() -> None:
    assert parse_retry_after_seconds("3") == 3.0


def test_translation_server_uses_trusted_local_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _amount: int | None = None) -> bytes:
            return b"[]"

    def fake_safe_urlopen(
        request: urllib.request.Request,
        **kwargs: object,
    ) -> Response:
        captured.update(kwargs)
        captured["url"] = request.full_url
        return Response()

    monkeypatch.setattr(provider_http, "safe_urlopen", fake_safe_urlopen)

    result = TranslationServerClient("http://localhost:1969").search("10.1000/test")

    assert result == []
    assert captured["url"] == "http://localhost:1969/search"
    assert captured["max_redirects"] == 0
    assert captured["allow_private_networks"] is True
    assert captured["allow_loopback"] is True


def test_bounded_reader_rejects_declared_oversize_without_reading() -> None:
    class Response:
        headers = {"Content-Length": "11"}

        def read(self, _amount: int | None = None) -> bytes:
            pytest.fail("declared oversize response must not be read")

    with pytest.raises(RuntimeError, match="declares 11 bytes; limit is 10"):
        provider_http.read_response_bytes(Response(), max_bytes=10)


def test_bounded_reader_rejects_streamed_oversize() -> None:
    requested: list[int] = []

    class Response:
        headers: dict[str, str] = {}

        def read(self, amount: int) -> bytes:
            requested.append(amount)
            return b"x" * amount

    with pytest.raises(RuntimeError, match="exceeds 10 bytes"):
        provider_http.read_response_bytes(Response(), max_bytes=10)

    assert requested == [11]
