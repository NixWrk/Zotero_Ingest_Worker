from __future__ import annotations

import urllib.error

import pytest

from zotero_metadata_enrichment import provider_http
from zotero_metadata_enrichment.provider_http import HostThrottle, parse_retry_after_seconds, register_retry_after_from_http_error


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
