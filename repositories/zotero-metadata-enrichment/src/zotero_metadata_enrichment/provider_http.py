from __future__ import annotations

import email.utils
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any


_RETRY_AFTER_STATUS_CODES = {429, 503}
_DEFAULT_RETRY_AFTER_SECONDS = {
    429: 60.0,
    503: 15.0,
}
_DEFAULT_MIN_INTERVAL_SECONDS = 0.1
_HOST_MIN_INTERVAL_SECONDS = {
    "api.crossref.org": 0.2,
    "api.openalex.org": 0.1,
    "api.unpaywall.org": 0.2,
    "api.semanticscholar.org": 1.0,
    "eutils.ncbi.nlm.nih.gov": 0.34,
    "www.ncbi.nlm.nih.gov": 0.34,
    "arxiv.org": 1.0,
}


class HostThrottle:
    def __init__(
        self,
        *,
        clock: Any = time.monotonic,
        sleeper: Any = time.sleep,
    ) -> None:
        self._clock = clock
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._last_started_at: dict[str, float] = {}
        self._cooldown_until: dict[str, float] = {}

    def wait(self, host: str, *, min_interval_seconds: float) -> None:
        host = normalize_host(host)
        if not host:
            return
        min_interval_seconds = max(0.0, float(min_interval_seconds))
        while True:
            with self._lock:
                now = float(self._clock())
                wait_until = max(
                    self._cooldown_until.get(host, 0.0),
                    self._last_started_at.get(host, -min_interval_seconds) + min_interval_seconds,
                )
                if now >= wait_until:
                    self._last_started_at[host] = now
                    return
                wait_seconds = wait_until - now
            self._sleeper(wait_seconds)

    def set_cooldown(self, host: str, seconds: float) -> None:
        host = normalize_host(host)
        if not host:
            return
        seconds = max(0.0, float(seconds))
        with self._lock:
            until = float(self._clock()) + seconds
            self._cooldown_until[host] = max(self._cooldown_until.get(host, 0.0), until)

    def reset(self) -> None:
        with self._lock:
            self._last_started_at.clear()
            self._cooldown_until.clear()


_GLOBAL_HOST_THROTTLE = HostThrottle()


def read_json_object(
    request: urllib.request.Request,
    *,
    timeout: float,
    min_interval_seconds: float | None = None,
    error_label: str = "",
) -> dict[str, Any]:
    with throttled_urlopen(
        request,
        timeout=timeout,
        min_interval_seconds=min_interval_seconds,
    ) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected JSON object from {error_label or request.full_url}")
    return payload


def read_json_list(
    request: urllib.request.Request,
    *,
    timeout: float,
    min_interval_seconds: float | None = None,
    error_label: str = "",
) -> list[Any]:
    with throttled_urlopen(
        request,
        timeout=timeout,
        min_interval_seconds=min_interval_seconds,
    ) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, list):
        raise RuntimeError(f"Expected JSON list from {error_label or request.full_url}")
    return payload


def read_text(
    request: urllib.request.Request,
    *,
    timeout: float,
    min_interval_seconds: float | None = None,
) -> str:
    with throttled_urlopen(
        request,
        timeout=timeout,
        min_interval_seconds=min_interval_seconds,
    ) as response:
        charset = response_charset(response.headers)
        return response.read().decode(charset, errors="replace")


def throttled_urlopen(
    request: urllib.request.Request,
    *,
    timeout: float,
    min_interval_seconds: float | None = None,
) -> Any:
    host = request_host(request)
    interval = (
        default_min_interval_seconds(host)
        if min_interval_seconds is None
        else max(0.0, float(min_interval_seconds))
    )
    _GLOBAL_HOST_THROTTLE.wait(host, min_interval_seconds=interval)
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        register_retry_after_from_http_error(exc)
        raise


def register_retry_after_from_http_error(exc: urllib.error.HTTPError) -> float | None:
    if exc.code not in _RETRY_AFTER_STATUS_CODES:
        return None
    host = normalize_host(urllib.parse.urlparse(str(exc.url or "")).netloc)
    if not host:
        return None
    seconds = retry_after_seconds_from_http_error(exc)
    if seconds is None:
        seconds = _DEFAULT_RETRY_AFTER_SECONDS.get(exc.code, 0.0)
    _GLOBAL_HOST_THROTTLE.set_cooldown(host, seconds)
    return seconds


def retry_after_seconds_from_http_error(exc: urllib.error.HTTPError) -> float | None:
    return retry_after_seconds_from_headers(getattr(exc, "headers", None))


def retry_after_seconds_from_headers(headers: Any) -> float | None:
    if headers is None:
        return None
    try:
        raw = headers.get("Retry-After")
    except AttributeError:
        raw = None
    return parse_retry_after_seconds(raw)


def parse_retry_after_seconds(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        target = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    return max(0.0, (target - datetime.now(UTC)).total_seconds())


def request_host(request: urllib.request.Request) -> str:
    return normalize_host(urllib.parse.urlparse(request.full_url).netloc)


def normalize_host(host: str) -> str:
    return str(host or "").split("@")[-1].split(":")[0].strip().lower()


def default_min_interval_seconds(host: str) -> float:
    return _HOST_MIN_INTERVAL_SECONDS.get(normalize_host(host), _DEFAULT_MIN_INTERVAL_SECONDS)


def response_charset(headers: Any) -> str:
    try:
        return headers.get_content_charset() or "utf-8"
    except AttributeError:
        pass
    try:
        content_type = str(headers.get("Content-Type") or headers.get("content-type") or "")
    except AttributeError:
        return "utf-8"
    for part in content_type.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name.casefold() == "charset" and value.strip():
            return value.strip()
    return "utf-8"
