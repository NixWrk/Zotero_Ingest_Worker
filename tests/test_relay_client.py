from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from zotero_ingest_worker.relay_client import ZoteroRelayClient


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_relay_client_create_html_sibling_uses_custom_dedupe(monkeypatch: Any, tmp_path: Path) -> None:
    source = tmp_path / "article.html"
    source.write_text("<html></html>", encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_request_json(self: ZoteroRelayClient, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"ok": True, "newAttachmentKey": "HTML1234"}

    monkeypatch.setattr(ZoteroRelayClient, "request_json", fake_request_json)
    client = ZoteroRelayClient(SimpleNamespace(zotero_relay_url="http://relay"))
    attachment = SimpleNamespace(key="PDF1234", library_id="LIB1", state_key="LIB1_PDF1234")

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
    assert payload["deduplicationKey"] == "trash-empty-html-parent:LIB1:ITEM1234:HTML1,HTML2"


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
    monkeypatch.setattr("zotero_ingest_worker.relay_client.time.sleep", lambda _seconds: None)
    client = ZoteroRelayClient(
        SimpleNamespace(
            zotero_relay_url="http://relay",
            request_timeout_seconds=10,
            zotero_relay_request_attempts=2,
            zotero_relay_retry_delay_seconds=0,
        )
    )

    result = client.request_json(method="POST", path="/items", payload={}, error_label="relay")

    assert result == {"ok": True, "retried": True}
    assert calls["count"] == 2


def test_request_json_does_not_retry_non_transient_relay_result(monkeypatch: Any) -> None:
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
        client.request_json(method="POST", path="/items", payload={}, error_label="relay")

    assert calls["count"] == 1
