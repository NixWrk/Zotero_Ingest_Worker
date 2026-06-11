from __future__ import annotations

from typing import Any

from zotero_ingest_worker.config import from_env
from zotero_ingest_worker.service_actions import POST_ACTION_PATHS, run_post_action


class _FakeFullRunManager:
    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"route": "full_run_start", "payload": payload}

    def status(self, run_id: str | None = None, *, event_limit: int = 50) -> dict[str, Any]:
        return {"route": "full_run_status", "run_id": run_id, "event_limit": event_limit}

    def stop(self, run_id: str | None = None) -> dict[str, Any]:
        return {"route": "full_run_stop", "run_id": run_id}


def test_post_action_paths_are_ingest_only() -> None:
    assert "/api/zotero/metadata/enrich/backlog-scan" in POST_ACTION_PATHS
    assert "/api/zotero/full-text/backlog-scan" in POST_ACTION_PATHS
    assert "/api/zotero/ocr/process-target" not in POST_ACTION_PATHS
    assert "/api/zotero/html/queue/enqueue-target" not in POST_ACTION_PATHS


def test_run_post_action_routes_metadata_queue(monkeypatch: Any) -> None:
    class FakeMetadataProcessor:
        def __init__(self, _config: Any) -> None:
            pass

        def queue(self, **kwargs: Any) -> dict[str, Any]:
            return {"route": "metadata_queue", "kwargs": kwargs}

    monkeypatch.setattr(
        "zotero_ingest_worker.service_actions.ZoteroMetadataProcessor",
        FakeMetadataProcessor,
    )

    result = run_post_action(
        "/api/zotero/metadata/queue/summary",
        from_env(load_file=False),
        {"type": "full_text", "status": "queued,failed", "limit": "5"},
        _FakeFullRunManager(),
    )

    assert result == {
        "route": "metadata_queue",
        "kwargs": {
            "job_type": "full_text",
            "statuses": {"queued", "failed"},
            "limit": 5,
        },
    }


def test_run_post_action_routes_full_text_backlog_with_auto_drain(monkeypatch: Any) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeMetadataProcessor:
        def __init__(self, _config: Any) -> None:
            pass

        def full_text_backlog_scan(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(("full_text_backlog_scan", kwargs))
            return {"queued": 1}

        def drain_full_text_queue(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(("drain_full_text_queue", kwargs))
            return {"processed": 1}

    monkeypatch.setattr(
        "zotero_ingest_worker.service_actions.ZoteroMetadataProcessor",
        FakeMetadataProcessor,
    )

    result = run_post_action(
        "/api/zotero/full-text/backlog-scan",
        from_env(load_file=False),
        {
            "max_items": "20",
            "limit": "3",
            "force": True,
            "library_id": "LIB1",
            "data_dir": "/tmp/zotero",
            "collection": "C1",
            "auto_drain": True,
            "drain_limit": "7",
            "dry_run": True,
        },
        _FakeFullRunManager(),
    )

    assert result == {"queued": 1, "drain": {"processed": 1}}
    assert calls == [
        (
            "full_text_backlog_scan",
            {
                "max_items": 20,
                "limit": 3,
                "force": True,
                "library_id": "LIB1",
                "data_dir": "/tmp/zotero",
                "collection": "C1",
            },
        ),
        ("drain_full_text_queue", {"limit": 7, "dry_run": True}),
    ]


def test_run_post_action_routes_full_run_status() -> None:
    result = run_post_action(
        "/api/zotero/pipeline/full-run/status",
        from_env(load_file=False),
        {"run_id": "RUN1", "event_limit": "7"},
        _FakeFullRunManager(),
    )

    assert result == {"route": "full_run_status", "run_id": "RUN1", "event_limit": 7}
