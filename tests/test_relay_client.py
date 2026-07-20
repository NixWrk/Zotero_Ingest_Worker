from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import zotero_ingest_worker.relay_client as relay_client_module
from zotero_ingest_worker.relay_client import ZoteroRelayClient


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        body = json.dumps(self.payload).encode("utf-8")
        return body if size < 0 else body[:size]


def test_relay_client_create_html_sibling_uses_custom_dedupe(
    monkeypatch: Any, tmp_path: Path
) -> None:
    source = tmp_path / "article.html"
    source.write_text("<html></html>", encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_request_json(self: ZoteroRelayClient, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"ok": True, "newAttachmentKey": "HTML1234"}

    monkeypatch.setattr(ZoteroRelayClient, "request_json", fake_request_json)
    client = ZoteroRelayClient(SimpleNamespace(zotero_relay_url="http://relay"))
    attachment = SimpleNamespace(
        key="PDF1234", library_id="LIB1", state_key="LIB1_PDF1234"
    )

    result = client.create_html_sibling(
        attachment=attachment,
        source_path=source,
        filename="article [EN HTML].html",
        title="Article [EN HTML]",
        deduplication_key="html-sibling:LIB1_PDF1234:en:1",
        error_label="html sibling",
    )

    assert result["newAttachmentKey"] == "HTML1234"
    assert captured["path"] == "/attachments/PDF1234/siblings/html"
    assert captured["error_label"] == "html sibling"
    payload = captured["payload"]
    assert payload["libraryId"] == "LIB1"
    assert payload["deduplicationKey"] == "html-sibling:LIB1_PDF1234:en:1"


def test_relay_client_ensure_parent_payload(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_request_json(self: ZoteroRelayClient, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"ok": True, "parentItemKey": "ITEM1234"}

    monkeypatch.setattr(ZoteroRelayClient, "request_json", fake_request_json)
    client = ZoteroRelayClient(SimpleNamespace(zotero_relay_url="http://relay"))
    attachment = SimpleNamespace(
        key="PDF1234",
        library_id="LIB1",
        state_key="LIB1_PDF1234",
        filename="paper.pdf",
    )

    result = client.ensure_parent(attachment)

    assert result["parentItemKey"] == "ITEM1234"
    assert captured["path"] == "/attachments/PDF1234/parent/ensure"
    payload = captured["payload"]
    assert payload["title"] == "paper"
    assert payload["deduplicationKey"] == "ensure-parent:LIB1_PDF1234"


def test_relay_client_trash_generated_parent_payload(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_request_json(self: ZoteroRelayClient, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"ok": True, "trashed": True}

    monkeypatch.setattr(ZoteroRelayClient, "request_json", fake_request_json)
    client = ZoteroRelayClient(SimpleNamespace(zotero_relay_url="http://relay"))

    result = client.trash_generated_html_parent(
        library_id="LIB1",
        parent_key="ITEM1234",
        deleted_child_keys=["HTML2", "HTML1"],
        dry_run=False,
    )

    assert result["trashed"] is True
    assert captured["path"] == "/items/ITEM1234/trash-if-generated-html-only"
    payload = captured["payload"]
    assert payload["deletedChildKeys"] == ["HTML2", "HTML1"]
    assert (
        payload["deduplicationKey"]
        == "trash-empty-html-parent:LIB1:ITEM1234:HTML1,HTML2"
    )


def test_request_json_retries_transient_relay_network_result(monkeypatch: Any) -> None:
    calls = {"count": 0}

    def fake_urlopen(_request: object, *, timeout: int) -> _FakeResponse:
        calls["count"] += 1
        assert timeout == 10
        if calls["count"] == 1:
            return _FakeResponse(
                {
                    "ok": False,
                    "error": {
                        "code": "WEB_API_REQUEST_FAILED",
                        "message": "Zotero Web API request failed.",
                        "details": {
                            "status": None,
                            "body": "[Errno -3] Temporary failure in name resolution",
                        },
                    },
                }
            )
        return _FakeResponse({"ok": True, "retried": True})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(
        "zotero_ingest_worker.relay_client.time.sleep", lambda _seconds: None
    )
    client = ZoteroRelayClient(
        SimpleNamespace(
            zotero_relay_url="http://relay",
            request_timeout_seconds=10,
            zotero_relay_request_attempts=2,
            zotero_relay_retry_delay_seconds=0,
        )
    )

    result = client.request_json(
        method="POST", path="/items", payload={}, error_label="relay"
    )

    assert result == {"ok": True, "retried": True}
    assert calls["count"] == 2


def test_request_json_retries_relay_partial_failure_207(monkeypatch: Any) -> None:
    calls = {"count": 0}

    def fake_urlopen(_request: object, *, timeout: int) -> _FakeResponse:
        del timeout
        calls["count"] += 1
        if calls["count"] == 1:
            return _FakeResponse(
                {
                    "ok": False,
                    "error": {
                        "code": "PARTIAL_FAILURE",
                        "message": "Local write succeeded but WebDAV upload failed.",
                        "details": {"status": 207},
                    },
                }
            )
        return _FakeResponse({"ok": True, "retried": True})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(
        "zotero_ingest_worker.relay_client.time.sleep", lambda _seconds: None
    )
    client = ZoteroRelayClient(
        SimpleNamespace(
            zotero_relay_url="http://relay",
            request_timeout_seconds=10,
            zotero_relay_request_attempts=2,
            zotero_relay_retry_delay_seconds=0,
        )
    )

    result = client.request_json(
        method="POST", path="/items", payload={}, error_label="relay"
    )

    assert result == {"ok": True, "retried": True}
    assert calls["count"] == 2


def test_request_json_does_not_retry_non_transient_relay_result(
    monkeypatch: Any,
) -> None:
    calls = {"count": 0}

    def fake_urlopen(_request: object, *, timeout: int) -> _FakeResponse:
        del timeout
        calls["count"] += 1
        return _FakeResponse({"ok": False, "error": {"code": "WEB_API_NOT_CONFIGURED"}})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = ZoteroRelayClient(
        SimpleNamespace(
            zotero_relay_url="http://relay",
            request_timeout_seconds=10,
            zotero_relay_request_attempts=3,
            zotero_relay_retry_delay_seconds=0,
        )
    )

    with pytest.raises(RuntimeError, match="WEB_API_NOT_CONFIGURED"):
        client.request_json(
            method="POST", path="/items", payload={}, error_label="relay"
        )

    assert calls["count"] == 1


@pytest.mark.parametrize(
    "payload",
    [None, [], "false", {}, {"ok": "false"}],
    ids=["null", "list", "string", "missing-ok", "non-boolean-ok"],
)
def test_request_json_rejects_malformed_relay_response(
    payload: object,
    monkeypatch: Any,
) -> None:
    calls = {"count": 0}

    def fake_urlopen(_request: object, *, timeout: int) -> _FakeResponse:
        del timeout
        calls["count"] += 1
        return _FakeResponse(payload)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = ZoteroRelayClient(
        SimpleNamespace(
            zotero_relay_url="http://relay",
            request_timeout_seconds=10,
            zotero_relay_request_attempts=1,
        )
    )

    with pytest.raises(RuntimeError, match="INVALID_RELAY_RESPONSE"):
        client.request_json(
            method="POST",
            path="/items",
            payload={},
            error_label="relay",
        )

    assert calls["count"] == 1


class _RawResponse:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def __enter__(self) -> "_RawResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self.data if size < 0 else self.data[:size]


def test_request_json_wraps_invalid_success_body(monkeypatch: Any) -> None:
    calls = {"count": 0}

    def fake_urlopen(_request: object, *, timeout: int) -> _RawResponse:
        del timeout
        calls["count"] += 1
        return _RawResponse(b"not-json")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = ZoteroRelayClient(
        SimpleNamespace(
            zotero_relay_url="http://relay",
            request_timeout_seconds=10,
            zotero_relay_request_attempts=3,
            zotero_relay_retry_delay_seconds=0,
        )
    )

    with pytest.raises(RuntimeError, match="INVALID_RELAY_RESPONSE"):
        client.request_json(
            method="POST",
            path="/items",
            payload={},
            error_label="relay",
        )

    assert calls["count"] == 1


def test_request_json_retries_bare_timeout(monkeypatch: Any) -> None:
    calls = {"count": 0}

    def fake_urlopen(_request: object, *, timeout: int) -> _FakeResponse:
        del timeout
        calls["count"] += 1
        if calls["count"] == 1:
            raise TimeoutError("timed out")
        return _FakeResponse({"ok": True, "retried": True})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(
        "zotero_ingest_worker.relay_client.time.sleep", lambda _seconds: None
    )
    client = ZoteroRelayClient(
        SimpleNamespace(
            zotero_relay_url="http://relay",
            request_timeout_seconds=10,
            zotero_relay_request_attempts=2,
            zotero_relay_retry_delay_seconds=0,
        )
    )

    result = client.request_json(
        method="POST",
        path="/items",
        payload={},
        error_label="relay",
    )

    assert result == {"ok": True, "retried": True}
    assert calls["count"] == 2


class _TrackingBody:
    def __init__(self, payload: bytes | object) -> None:
        self.payload = payload
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes | object:
        self.read_sizes.append(size)
        if isinstance(self.payload, bytes):
            return self.payload if size < 0 else self.payload[:size]
        return self.payload

    def close(self) -> None:
        return None


class _TrackingResponse:
    def __init__(self, body: _TrackingBody) -> None:
        self.body = body

    def __enter__(self) -> "_TrackingResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes | object:
        return self.body.read(size)


def test_request_json_bounds_success_response_before_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    max_bytes = 32
    body = _TrackingBody(b"x" * (max_bytes + 20))
    monkeypatch.setattr(
        relay_client_module,
        "MAX_RELAY_RESPONSE_BYTES",
        max_bytes,
        raising=False,
    )
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: _TrackingResponse(body),
    )
    client = ZoteroRelayClient(
        SimpleNamespace(
            zotero_relay_url="http://relay",
            request_timeout_seconds=10,
            zotero_relay_request_attempts=1,
        )
    )

    with pytest.raises(RuntimeError, match="RELAY_RESPONSE_TOO_LARGE"):
        client.request_json(
            method="POST",
            path="/items",
            payload={},
            error_label="relay",
        )

    assert body.read_sizes == [max_bytes + 1]


def test_request_json_accepts_response_at_exact_byte_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"ok": True, "boundary": True}
    raw = json.dumps(payload).encode("utf-8")
    body = _TrackingBody(raw)
    monkeypatch.setattr(
        relay_client_module,
        "MAX_RELAY_RESPONSE_BYTES",
        len(raw),
    )
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: _TrackingResponse(body),
    )
    client = ZoteroRelayClient(
        SimpleNamespace(
            zotero_relay_url="http://relay",
            request_timeout_seconds=10,
            zotero_relay_request_attempts=1,
        )
    )

    assert (
        client.request_json(
            method="POST",
            path="/items",
            payload={},
            error_label="relay",
        )
        == payload
    )
    assert body.read_sizes == [len(raw) + 1]


def test_request_json_bounds_http_error_response_before_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    max_bytes = 32
    body = _TrackingBody(b"x" * (max_bytes + 20))
    error = urllib.error.HTTPError(
        "http://relay/items",
        400,
        "Bad Request",
        hdrs=None,
        fp=body,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        relay_client_module,
        "MAX_RELAY_RESPONSE_BYTES",
        max_bytes,
        raising=False,
    )
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )
    client = ZoteroRelayClient(
        SimpleNamespace(
            zotero_relay_url="http://relay",
            request_timeout_seconds=10,
            zotero_relay_request_attempts=1,
        )
    )

    with pytest.raises(RuntimeError, match="RELAY_RESPONSE_TOO_LARGE"):
        client.request_json(
            method="POST",
            path="/items",
            payload={},
            error_label="relay",
        )

    assert body.read_sizes == [max_bytes + 1]


def test_request_json_wraps_non_bytes_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _TrackingBody("not-bytes")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: _TrackingResponse(body),
    )
    client = ZoteroRelayClient(
        SimpleNamespace(
            zotero_relay_url="http://relay",
            request_timeout_seconds=10,
            zotero_relay_request_attempts=1,
        )
    )

    with pytest.raises(RuntimeError, match="INVALID_RELAY_RESPONSE"):
        client.request_json(
            method="POST",
            path="/items",
            payload={},
            error_label="relay",
        )


def test_request_json_wraps_deeply_nested_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = (b"[" * 2_000) + (b"]" * 2_000)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: _RawResponse(raw),
    )
    client = ZoteroRelayClient(
        SimpleNamespace(
            zotero_relay_url="http://relay",
            request_timeout_seconds=10,
            zotero_relay_request_attempts=1,
        )
    )

    with pytest.raises(RuntimeError, match="INVALID_RELAY_RESPONSE"):
        client.request_json(method="GET", path="/health", error_label="relay")


def test_request_json_retries_response_read_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"count": 0}

    class FailingReadResponse(_FakeResponse):
        def read(self, size: int = -1) -> bytes:
            del size
            raise OSError("response stream closed")

    def fake_urlopen(_request: object, *, timeout: int) -> _FakeResponse:
        del timeout
        calls["count"] += 1
        if calls["count"] == 1:
            return FailingReadResponse({"ok": True})
        return _FakeResponse({"ok": True, "retried": True})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(relay_client_module.time, "sleep", lambda _seconds: None)
    client = ZoteroRelayClient(
        SimpleNamespace(
            zotero_relay_url="http://relay",
            request_timeout_seconds=10,
            zotero_relay_request_attempts=2,
            zotero_relay_retry_delay_seconds=0,
        )
    )

    assert client.request_json(
        method="POST",
        path="/items",
        payload={},
        error_label="relay",
    ) == {"ok": True, "retried": True}
    assert calls["count"] == 2


class _FailingBody:
    def __init__(self) -> None:
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        raise OSError("response stream closed")

    def close(self) -> None:
        return None


def test_request_json_retries_http_500_body_read_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"count": 0}
    body = _FailingBody()

    def fake_urlopen(_request: object, *, timeout: int) -> _FakeResponse:
        del timeout
        calls["count"] += 1
        if calls["count"] == 1:
            raise urllib.error.HTTPError(
                "http://relay/items",
                500,
                "Internal Server Error",
                hdrs=None,
                fp=body,  # type: ignore[arg-type]
            )
        return _FakeResponse({"ok": True, "retried": True})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(relay_client_module.time, "sleep", lambda _seconds: None)
    client = ZoteroRelayClient(
        SimpleNamespace(
            zotero_relay_url="http://relay",
            request_timeout_seconds=10,
            zotero_relay_request_attempts=2,
            zotero_relay_retry_delay_seconds=0,
        )
    )

    assert client.request_json(
        method="POST",
        path="/items",
        payload={},
        error_label="relay",
    ) == {"ok": True, "retried": True}
    assert calls["count"] == 2
    assert body.read_sizes == [relay_client_module.MAX_RELAY_RESPONSE_BYTES + 1]


def test_request_json_does_not_retry_http_400_body_read_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"count": 0}
    bodies: list[_FailingBody] = []

    def fake_urlopen(_request: object, *, timeout: int) -> _FakeResponse:
        del timeout
        calls["count"] += 1
        body = _FailingBody()
        bodies.append(body)
        raise urllib.error.HTTPError(
            "http://relay/items",
            400,
            "Bad Request",
            hdrs=None,
            fp=body,  # type: ignore[arg-type]
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(relay_client_module.time, "sleep", lambda _seconds: None)
    client = ZoteroRelayClient(
        SimpleNamespace(
            zotero_relay_url="http://relay",
            request_timeout_seconds=10,
            zotero_relay_request_attempts=3,
            zotero_relay_retry_delay_seconds=0,
        )
    )

    with pytest.raises(RuntimeError, match="RELAY_RESPONSE_READ_FAILED"):
        client.request_json(
            method="POST",
            path="/items",
            payload={},
            error_label="relay",
        )

    assert calls["count"] == 1
    assert len(bodies) == 1


def test_request_json_uses_host_fallback_after_container_dns_failure(
    monkeypatch: Any,
) -> None:
    urls: list[str] = []

    def fake_urlopen(request: Any, *, timeout: int) -> _FakeResponse:
        del timeout
        urls.append(str(request.full_url))
        if "zotero-file-relay" in str(request.full_url):
            raise urllib.error.URLError("name resolution")
        return _FakeResponse({"ok": True, "fallback": True})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = ZoteroRelayClient(
        SimpleNamespace(
            zotero_relay_url="http://zotero-file-relay:23118",
            request_timeout_seconds=10,
            zotero_relay_request_attempts=1,
        )
    )

    result = client.request_json(
        method="POST",
        path="/items",
        payload={},
        error_label="relay",
    )

    assert result == {"ok": True, "fallback": True}
    assert urls == [
        "http://zotero-file-relay:23118/items",
        "http://127.0.0.1:23118/items",
    ]
