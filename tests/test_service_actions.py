from __future__ import annotations

from typing import Any

import pytest

from zotero_ingest_worker.config import from_env
from zotero_ingest_worker.service_actions import (
    MAX_ACTION_RESULT_DEPTH,
    MAX_DRAIN_ITEMS,
    MAX_FILTER_ITEMS,
    MAX_PARENT_FILTER_KEYS,
    MAX_QUEUE_OFFSET,
    MAX_QUEUE_PAGE_ITEMS,
    POST_ACTION_PATHS,
    run_post_action,
)


class _FakeFullRunManager:
    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"route": "full_run_start", "payload": payload}

    def status(
        self, run_id: str | None = None, *, event_limit: int = 50
    ) -> dict[str, Any]:
        return {
            "route": "full_run_status",
            "run_id": run_id,
            "event_limit": event_limit,
        }

    def stop(self, run_id: str | None = None) -> dict[str, Any]:
        return {"route": "full_run_stop", "run_id": run_id}


def test_post_action_paths_are_ingest_only() -> None:
    assert "/api/zotero/metadata/enrich/backlog-scan" in POST_ACTION_PATHS
    assert "/api/zotero/full-text/backlog-scan" in POST_ACTION_PATHS
    assert "/api/zotero/ocr/process-target" not in POST_ACTION_PATHS
    assert "/api/zotero/html/queue/enqueue-target" not in POST_ACTION_PATHS


def test_run_post_action_routes_metadata_queue(monkeypatch: Any) -> None:
    calls: list[dict[str, Any]] = []

    class FakeMetadataProcessor:
        def __init__(self, _config: Any) -> None:
            pass

        def queue(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"route": "metadata_queue"}

    monkeypatch.setattr(
        "zotero_ingest_worker.service_actions.ZoteroMetadataProcessor",
        FakeMetadataProcessor,
    )

    result = run_post_action(
        "/api/zotero/metadata/queue/summary",
        from_env(load_file=False),
        {
            "type": "full_text",
            "status": "queued,failed",
            "limit": 5,
            "offset": 7,
            "library_ids": ["LIB2", "", "LIB1", "LIB1"],
        },
        _FakeFullRunManager(),
    )

    assert result == {"route": "metadata_queue"}
    assert calls == [
        {
            "job_type": "full_text",
            "statuses": {"queued", "failed_final", "failed_retryable"},
            "limit": 5,
            "offset": 7,
            "library_ids": {"LIB1", "LIB2"},
        }
    ]


def test_run_post_action_metadata_queue_preserves_zero_limit(monkeypatch: Any) -> None:
    class FakeMetadataProcessor:
        def __init__(self, _config: Any) -> None:
            pass

        def queue(self, **kwargs: Any) -> dict[str, Any]:
            return kwargs

    monkeypatch.setattr(
        "zotero_ingest_worker.service_actions.ZoteroMetadataProcessor",
        FakeMetadataProcessor,
    )

    result = run_post_action(
        "/api/zotero/metadata/queue/summary",
        from_env(load_file=False),
        {"type": "full_text", "limit": 0},
        _FakeFullRunManager(),
    )

    assert result == {
        "job_type": "full_text",
        "statuses": None,
        "limit": 0,
        "offset": 0,
        "library_ids": None,
    }


def test_run_post_action_routes_full_text_backlog_with_auto_drain(
    monkeypatch: Any,
) -> None:
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
            "max_items": 20,
            "limit": 3,
            "force": True,
            "library_id": "LIB1",
            "data_dir": "/tmp/zotero",
            "collection": "C1",
            "only_parent_keys_by_library": {
                "LIB1": ["PARENT2", "PARENT1"],
                "LIB2": [],
            },
            "auto_drain": True,
            "drain_limit": 7,
            "dry_run": True,
            "require_relay": False,
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
                "only_parent_keys_by_library": {
                    "LIB1": ["PARENT1", "PARENT2"],
                    "LIB2": [],
                },
            },
        ),
        (
            "drain_full_text_queue",
            {"limit": 7, "dry_run": True, "require_relay": False},
        ),
    ]


def test_run_post_action_routes_source_html_cleanup(monkeypatch: Any) -> None:
    calls: list[dict[str, Any]] = []

    class FakeMetadataProcessor:
        def __init__(self, _config: Any) -> None:
            pass

        def source_html_cleanup(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"route": "source_html_cleanup"}

    monkeypatch.setattr(
        "zotero_ingest_worker.service_actions.ZoteroMetadataProcessor",
        FakeMetadataProcessor,
    )

    result = run_post_action(
        "/api/zotero/source-html/cleanup",
        from_env(load_file=False),
        {
            "max_items": 50,
            "limit": 10,
            "dry_run": False,
            "confirm": True,
            "delete_webdav": True,
            "library_id": "LIB1",
            "data_dir": "/tmp/zotero",
            "collection": "C1",
        },
        _FakeFullRunManager(),
    )

    assert result == {"route": "source_html_cleanup"}
    assert calls == [
        {
            "max_items": 50,
            "limit": 10,
            "dry_run": False,
            "confirm": True,
            "delete_webdav": True,
            "library_id": "LIB1",
            "data_dir": "/tmp/zotero",
            "collection": "C1",
        }
    ]


def test_run_post_action_routes_scihub_backlog_with_parent_filter(
    monkeypatch: Any,
) -> None:
    calls: list[dict[str, Any]] = []

    class FakeMetadataProcessor:
        def __init__(self, _config: Any) -> None:
            pass

        def scihub_pdf_backlog_scan(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"route": "scihub_pdf_backlog_scan"}

    monkeypatch.setattr(
        "zotero_ingest_worker.service_actions.ZoteroMetadataProcessor",
        FakeMetadataProcessor,
    )

    result = run_post_action(
        "/api/zotero/scihub-pdf/backlog-scan",
        from_env(load_file=False),
        {
            "limit": 9,
            "only_parent_keys_by_library": {"LIB1": ["PARENT2", "PARENT1"]},
        },
        _FakeFullRunManager(),
    )

    assert result == {"route": "scihub_pdf_backlog_scan"}
    assert calls == [
        {
            "max_items": None,
            "limit": 9,
            "force": False,
            "library_id": None,
            "data_dir": None,
            "collection": None,
            "only_parent_keys_by_library": {"LIB1": ["PARENT1", "PARENT2"]},
        }
    ]


def test_run_post_action_routes_full_run_status() -> None:
    result = run_post_action(
        "/api/zotero/pipeline/full-run/status",
        from_env(load_file=False),
        {"run_id": "RUN1", "event_limit": 7},
        _FakeFullRunManager(),
    )

    assert result == {"route": "full_run_status", "run_id": "RUN1", "event_limit": 7}


@pytest.mark.parametrize("event_limit", [-1, 1001])
def test_run_post_action_rejects_unbounded_full_run_event_limit(
    event_limit: int,
) -> None:
    with pytest.raises(ValueError, match="event_limit"):
        run_post_action(
            "/api/zotero/pipeline/full-run/status",
            from_env(load_file=False),
            {"run_id": "RUN1", "event_limit": event_limit},
            _FakeFullRunManager(),
        )


def test_run_post_action_allows_zero_full_run_event_limit() -> None:
    result = run_post_action(
        "/api/zotero/pipeline/full-run/status",
        from_env(load_file=False),
        {"run_id": "RUN1", "event_limit": 0},
        _FakeFullRunManager(),
    )

    assert result == {
        "route": "full_run_status",
        "run_id": "RUN1",
        "event_limit": 0,
    }


@pytest.mark.parametrize("field", ["max_items", "limit"])
def test_backlog_route_rejects_negative_optional_budget(field: str) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        run_post_action(
            "/api/zotero/full-text/backlog-scan",
            from_env(load_file=False),
            {field: -1},
            _FakeFullRunManager(),
        )


def test_source_html_cleanup_rejects_string_booleans_before_side_effect(
    monkeypatch: Any,
) -> None:
    class UnexpectedMetadataProcessor:
        def __init__(self, _config: Any) -> None:
            raise AssertionError(
                "Malformed booleans must fail before processor creation."
            )

    monkeypatch.setattr(
        "zotero_ingest_worker.service_actions.ZoteroMetadataProcessor",
        UnexpectedMetadataProcessor,
    )

    with pytest.raises(ValueError, match="dry_run must be a JSON boolean"):
        run_post_action(
            "/api/zotero/source-html/cleanup",
            from_env(load_file=False),
            {
                "dry_run": "false",
                "confirm": True,
                "delete_webdav": True,
            },
            _FakeFullRunManager(),
        )


def test_backlog_scan_rejects_numeric_boolean_before_side_effect(
    monkeypatch: Any,
) -> None:
    class UnexpectedMetadataProcessor:
        def __init__(self, _config: Any) -> None:
            raise AssertionError(
                "Malformed booleans must fail before processor creation."
            )

    monkeypatch.setattr(
        "zotero_ingest_worker.service_actions.ZoteroMetadataProcessor",
        UnexpectedMetadataProcessor,
    )

    with pytest.raises(ValueError, match="force must be a JSON boolean"):
        run_post_action(
            "/api/zotero/full-text/backlog-scan",
            from_env(load_file=False),
            {"force": 1, "auto_drain": False},
            _FakeFullRunManager(),
        )


def test_queue_retry_rejects_string_reset_attempts(monkeypatch: Any) -> None:
    class UnexpectedMetadataProcessor:
        def __init__(self, _config: Any) -> None:
            raise AssertionError(
                "Malformed booleans must fail before processor creation."
            )

    monkeypatch.setattr(
        "zotero_ingest_worker.service_actions.ZoteroMetadataProcessor",
        UnexpectedMetadataProcessor,
    )

    with pytest.raises(ValueError, match="reset_attempts must be a JSON boolean"):
        run_post_action(
            "/api/zotero/metadata/queue/retry",
            from_env(load_file=False),
            {"job_id": "JOB1", "reset_attempts": "false"},
            _FakeFullRunManager(),
        )


def test_numeric_payload_rejects_coercion_before_side_effect(
    monkeypatch: Any,
) -> None:
    class UnexpectedMetadataProcessor:
        def __init__(self, _config: Any) -> None:
            raise AssertionError(
                "Malformed numerics must fail before processor creation."
            )

    monkeypatch.setattr(
        "zotero_ingest_worker.service_actions.ZoteroMetadataProcessor",
        UnexpectedMetadataProcessor,
    )

    with pytest.raises(ValueError, match="max_items must be a JSON integer"):
        run_post_action(
            "/api/zotero/full-text/backlog-scan",
            from_env(load_file=False),
            {"max_items": "20", "force": False},
            _FakeFullRunManager(),
        )


def test_retry_failed_requires_exact_boolean_before_config_override(
    monkeypatch: Any,
) -> None:
    class UnexpectedMetadataProcessor:
        def __init__(self, _config: Any) -> None:
            raise AssertionError(
                "Malformed booleans must fail before processor creation."
            )

    monkeypatch.setattr(
        "zotero_ingest_worker.service_actions.ZoteroMetadataProcessor",
        UnexpectedMetadataProcessor,
    )

    with pytest.raises(ValueError, match="retry_failed must be a JSON boolean"):
        run_post_action(
            "/api/zotero/metadata/queue/summary",
            from_env(load_file=False),
            {"retry_failed": "false"},
            _FakeFullRunManager(),
        )


def test_role_guard_runs_before_boolean_payload_validation() -> None:
    with pytest.raises(PermissionError, match="metadata-only"):
        run_post_action(
            "/api/zotero/full-text/backlog-scan",
            from_env(load_file=False),
            {
                "force": "false",
                "auto_drain": "false",
            },
            _FakeFullRunManager(),
            role="metadata",
        )


@pytest.mark.parametrize(
    "malformed",
    [
        [],
        {1: ["PARENT1"]},
        {" ": ["PARENT1"]},
        {"LIB1": "PARENT1"},
        {"LIB1": [1]},
        {"LIB1": [" "]},
    ],
    ids=[
        "non-object",
        "non-string-library",
        "empty-library",
        "keys-not-array",
        "non-string-parent",
        "empty-parent",
    ],
)
def test_parent_key_filter_rejects_malformed_contract_before_side_effect(
    monkeypatch: Any,
    malformed: object,
) -> None:
    class UnexpectedMetadataProcessor:
        def __init__(self, _config: Any) -> None:
            raise AssertionError(
                "Malformed parent filters must fail before processor creation."
            )

    monkeypatch.setattr(
        "zotero_ingest_worker.service_actions.ZoteroMetadataProcessor",
        UnexpectedMetadataProcessor,
    )

    with pytest.raises(ValueError, match="only_parent_keys_by_library"):
        run_post_action(
            "/api/zotero/full-text/backlog-scan",
            from_env(load_file=False),
            {"only_parent_keys_by_library": malformed},
            _FakeFullRunManager(),
        )


@pytest.mark.parametrize(
    "field, malformed",
    [
        ("collection", 7),
        ("data_dir", []),
        ("job_id", {}),
        ("job_type", 1),
        ("library_id", False),
        ("policy", []),
        ("run_id", 7),
        ("type", {}),
    ],
)
def test_string_payload_rejects_coercion_before_side_effect(
    monkeypatch: Any,
    field: str,
    malformed: object,
) -> None:
    class UnexpectedMetadataProcessor:
        def __init__(self, _config: Any) -> None:
            raise AssertionError(
                "Malformed strings must fail before processor creation."
            )

    monkeypatch.setattr(
        "zotero_ingest_worker.service_actions.ZoteroMetadataProcessor",
        UnexpectedMetadataProcessor,
    )

    with pytest.raises(ValueError, match=rf"{field} must be a JSON string"):
        run_post_action(
            "/api/zotero/full-text/backlog-scan",
            from_env(load_file=False),
            {field: malformed},
            _FakeFullRunManager(),
        )


@pytest.mark.parametrize(
    "field, malformed",
    [
        ("status", {}),
        ("statuses", {}),
        ("statuses", [1]),
        ("library_ids", {}),
        ("library_ids", [1]),
    ],
)
def test_filter_payload_rejects_coercion_before_side_effect(
    monkeypatch: Any,
    field: str,
    malformed: object,
) -> None:
    class UnexpectedMetadataProcessor:
        def __init__(self, _config: Any) -> None:
            raise AssertionError(
                "Malformed filters must fail before processor creation."
            )

    monkeypatch.setattr(
        "zotero_ingest_worker.service_actions.ZoteroMetadataProcessor",
        UnexpectedMetadataProcessor,
    )

    with pytest.raises(
        ValueError,
        match=rf"{field} must be a JSON string or array of strings",
    ):
        run_post_action(
            "/api/zotero/metadata/queue/summary",
            from_env(load_file=False),
            {field: malformed},
            _FakeFullRunManager(),
        )


def test_source_html_cleanup_preserves_explicit_zero_budget(
    monkeypatch: Any,
) -> None:
    calls: list[dict[str, Any]] = []

    class FakeMetadataProcessor:
        def __init__(self, _config: Any) -> None:
            pass

        def source_html_cleanup(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"ok": True}

    monkeypatch.setattr(
        "zotero_ingest_worker.service_actions.ZoteroMetadataProcessor",
        FakeMetadataProcessor,
    )

    result = run_post_action(
        "/api/zotero/source-html/cleanup",
        from_env(load_file=False),
        {"max_items": 0, "limit": 0},
        _FakeFullRunManager(),
    )

    assert result == {"ok": True}
    assert calls == [
        {
            "max_items": 0,
            "limit": 0,
            "dry_run": True,
            "confirm": False,
            "delete_webdav": False,
            "library_id": None,
            "data_dir": None,
            "collection": None,
        }
    ]


@pytest.mark.parametrize(
    "path",
    [
        "/api/zotero/metadata/enrich/queue/drain",
        "/api/zotero/arxiv-html/queue/drain",
        "/api/zotero/full-text/queue/drain",
        "/api/zotero/researchgate-pdf/queue/drain",
        "/api/zotero/scihub-pdf/queue/drain",
    ],
)
@pytest.mark.parametrize("limit", [0, -1, MAX_DRAIN_ITEMS + 1])
def test_direct_drain_rejects_unsafe_limit_before_processor_creation(
    monkeypatch: Any,
    path: str,
    limit: int,
) -> None:
    class UnexpectedMetadataProcessor:
        def __init__(self, _config: Any) -> None:
            raise AssertionError("Unsafe drain limits must fail before side effects.")

    monkeypatch.setattr(
        "zotero_ingest_worker.service_actions.ZoteroMetadataProcessor",
        UnexpectedMetadataProcessor,
    )

    with pytest.raises(ValueError, match="limit"):
        run_post_action(
            path,
            from_env(load_file=False),
            {"limit": limit},
            _FakeFullRunManager(),
        )


@pytest.mark.parametrize("drain_limit", [0, -1, MAX_DRAIN_ITEMS + 1])
def test_backlog_rejects_unsafe_auto_drain_limit_even_when_disabled(
    monkeypatch: Any,
    drain_limit: int,
) -> None:
    class UnexpectedMetadataProcessor:
        def __init__(self, _config: Any) -> None:
            raise AssertionError("Unsafe drain limits must fail before side effects.")

    monkeypatch.setattr(
        "zotero_ingest_worker.service_actions.ZoteroMetadataProcessor",
        UnexpectedMetadataProcessor,
    )

    with pytest.raises(ValueError, match="drain_limit"):
        run_post_action(
            "/api/zotero/full-text/backlog-scan",
            from_env(load_file=False),
            {"auto_drain": False, "drain_limit": drain_limit},
            _FakeFullRunManager(),
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"limit": MAX_QUEUE_PAGE_ITEMS + 1},
        {"offset": -1},
        {"offset": MAX_QUEUE_OFFSET + 1},
    ],
)
def test_queue_summary_rejects_unsafe_page_bounds_before_processor_creation(
    monkeypatch: Any,
    payload: dict[str, int],
) -> None:
    class UnexpectedMetadataProcessor:
        def __init__(self, _config: Any) -> None:
            raise AssertionError("Unsafe queue bounds must fail before side effects.")

    monkeypatch.setattr(
        "zotero_ingest_worker.service_actions.ZoteroMetadataProcessor",
        UnexpectedMetadataProcessor,
    )

    with pytest.raises(ValueError, match="limit|offset"):
        run_post_action(
            "/api/zotero/metadata/queue/summary",
            from_env(load_file=False),
            payload,
            _FakeFullRunManager(),
        )


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"collection": "A\nB"}, "collection"),
        ({"data_dir": "x" * 4097}, "data_dir"),
        ({"library_id": "L\x00IB"}, "library_id"),
        ({"statuses": ["x" * 257]}, "statuses"),
        (
            {"library_ids": [f"LIB{i}" for i in range(MAX_FILTER_ITEMS + 1)]},
            "library_ids",
        ),
    ],
)
def test_action_strings_and_filters_are_bounded_and_printable(
    payload: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        run_post_action(
            "/api/zotero/metadata/queue/summary",
            from_env(load_file=False),
            payload,
            _FakeFullRunManager(),
        )


@pytest.mark.parametrize(
    "parent_filter",
    [
        {f"LIB{i}": [] for i in range(1025)},
        {"LIB1": [f"P{i}" for i in range(MAX_PARENT_FILTER_KEYS + 1)]},
        {"LIB1": ["P" * 257]},
        {"LIB\n1": ["P1"]},
        {"LIB1": ["P\x001"]},
    ],
)
def test_parent_key_filter_is_bounded_and_printable(
    parent_filter: dict[str, list[str]],
) -> None:
    with pytest.raises(ValueError, match="only_parent_keys_by_library"):
        run_post_action(
            "/api/zotero/full-text/backlog-scan",
            from_env(load_file=False),
            {"only_parent_keys_by_library": parent_filter},
            _FakeFullRunManager(),
        )


@pytest.mark.parametrize(
    "path, method_name, returned_job, expected_ok",
    [
        ("/api/zotero/metadata/queue/retry", "retry_metadata_job", {}, False),
        (
            "/api/zotero/metadata/queue/retry",
            "retry_metadata_job",
            {"job_id": "JOB1", "status": "failed_final"},
            False,
        ),
        (
            "/api/zotero/metadata/queue/retry",
            "retry_metadata_job",
            {"job_id": "JOB1", "status": "queued"},
            True,
        ),
        ("/api/zotero/metadata/queue/cancel", "cancel_metadata_job", {}, False),
        (
            "/api/zotero/metadata/queue/cancel",
            "cancel_metadata_job",
            {"job_id": "JOB1", "status": "running"},
            False,
        ),
        (
            "/api/zotero/metadata/queue/cancel",
            "cancel_metadata_job",
            {"job_id": "JOB1", "status": "cancelled"},
            True,
        ),
    ],
)
def test_retry_and_cancel_report_only_the_requested_terminal_state_as_ok(
    monkeypatch: Any,
    path: str,
    method_name: str,
    returned_job: dict[str, Any],
    expected_ok: bool,
) -> None:
    class FakeState:
        def retry_metadata_job(
            self,
            _job_id: str,
            *,
            reset_attempts: bool,
        ) -> dict[str, Any]:
            assert reset_attempts is False
            assert method_name == "retry_metadata_job"
            return returned_job

        def cancel_metadata_job(self, _job_id: str) -> dict[str, Any]:
            assert method_name == "cancel_metadata_job"
            return returned_job

    class FakeMetadataProcessor:
        def __init__(self, _config: Any) -> None:
            self.state = FakeState()

    monkeypatch.setattr(
        "zotero_ingest_worker.service_actions.ZoteroMetadataProcessor",
        FakeMetadataProcessor,
    )

    result = run_post_action(
        path,
        from_env(load_file=False),
        {"job_id": "JOB1"},
        _FakeFullRunManager(),
    )

    assert result["ok"] is expected_ok
    assert result["job"] == returned_job


@pytest.mark.parametrize(
    "path, payload",
    [
        (
            "/api/zotero/full-text/queue/drain",
            {"job_type": "enrich"},
        ),
        (
            "/api/zotero/metadata/queue/summary",
            {"type": "unknown"},
        ),
        (
            "/api/zotero/metadata/queue/summary",
            {"type": "full_text", "job_type": "enrich"},
        ),
        (
            "/api/zotero/metadata/queue/summary",
            {"statuses": ["queued", "typo"]},
        ),
        (
            "/api/zotero/metadata/queue/summary",
            {"status": "queued", "statuses": ["running"]},
        ),
        (
            "/api/zotero/metadata/queue/summary",
            {"library_id": "LIB1", "library_ids": ["LIB2"]},
        ),
    ],
)
def test_queue_and_drain_aliases_fail_closed_before_processor_creation(
    monkeypatch: Any,
    path: str,
    payload: dict[str, object],
) -> None:
    class UnexpectedMetadataProcessor:
        def __init__(self, _config: Any) -> None:
            raise AssertionError(
                "Invalid queue selectors must fail before side effects."
            )

    monkeypatch.setattr(
        "zotero_ingest_worker.service_actions.ZoteroMetadataProcessor",
        UnexpectedMetadataProcessor,
    )

    with pytest.raises(ValueError, match="job_type|type|status|library"):
        run_post_action(
            path,
            from_env(load_file=False),
            payload,
            _FakeFullRunManager(),
        )


@pytest.mark.parametrize(
    "path, payload",
    [
        ("/api/zotero/pipeline/full-run/start", {"drain_limt": 1}),
        ("/api/zotero/pipeline/full-run/status", {"event_limt": 1}),
        ("/api/zotero/pipeline/full-run/stop", {"force": False}),
        ("/api/zotero/metadata/queue/summary", {"include_items": True}),
        ("/api/zotero/full-text/backlog-scan", {"dryrun": True}),
        ("/api/zotero/full-text/queue/drain", {"require_webdav": True}),
        ("/api/zotero/source-html/cleanup", {"delete_webdavv": True}),
        ("/api/zotero/metadata/queue/retry", {"job_id": "J1", "reset": True}),
        (
            "/api/zotero/metadata/queue/cancel",
            {"job_id": "J1", "reset_attempts": False},
        ),
    ],
)
def test_post_actions_reject_unknown_fields_before_side_effects(
    monkeypatch: Any,
    path: str,
    payload: dict[str, object],
) -> None:
    class UnexpectedMetadataProcessor:
        def __init__(self, _config: Any) -> None:
            raise AssertionError("Unknown fields must fail before processor creation.")

    class UnexpectedFullRunManager:
        def start(self, _payload: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError("Unknown fields must not start a run.")

        def status(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("Unknown fields must not query a run.")

        def stop(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("Unknown fields must not stop a run.")

    monkeypatch.setattr(
        "zotero_ingest_worker.service_actions.ZoteroMetadataProcessor",
        UnexpectedMetadataProcessor,
    )

    with pytest.raises(ValueError, match="Unsupported.*field"):
        run_post_action(
            path,
            from_env(load_file=False),
            payload,
            UnexpectedFullRunManager(),  # type: ignore[arg-type]
        )


def test_post_action_rejects_non_mapping_result() -> None:
    class InvalidFullRunManager(_FakeFullRunManager):
        def status(self, *_args: Any, **_kwargs: Any) -> Any:
            return []

    with pytest.raises(RuntimeError, match="mapping"):
        run_post_action(
            "/api/zotero/pipeline/full-run/status",
            from_env(load_file=False),
            {},
            InvalidFullRunManager(),
        )


@pytest.mark.parametrize(
    "path, method_name",
    [
        ("/api/zotero/metadata/queue/summary", "queue"),
        ("/api/zotero/metadata/enrich/backlog-scan", "metadata_backlog_scan"),
        ("/api/zotero/metadata/enrich/queue/drain", "drain_metadata_queue"),
        ("/api/zotero/arxiv-html/backlog-scan", "arxiv_html_backlog_scan"),
        ("/api/zotero/arxiv-html/queue/drain", "drain_arxiv_html_queue"),
        ("/api/zotero/full-text/backlog-scan", "full_text_backlog_scan"),
        ("/api/zotero/full-text/queue/drain", "drain_full_text_queue"),
        ("/api/zotero/source-html/cleanup", "source_html_cleanup"),
        ("/api/zotero/scihub-pdf/backlog-scan", "scihub_pdf_backlog_scan"),
        (
            "/api/zotero/researchgate-pdf/queue/drain",
            "drain_researchgate_pdf_queue",
        ),
        ("/api/zotero/scihub-pdf/queue/drain", "drain_scihub_pdf_queue"),
    ],
)
def test_processor_routes_reject_non_mapping_results(
    monkeypatch: Any,
    path: str,
    method_name: str,
) -> None:
    class InvalidMetadataProcessor:
        def __init__(self, _config: Any) -> None:
            pass

        def __getattr__(self, name: str) -> Any:
            assert name == method_name
            return lambda **_kwargs: []

    monkeypatch.setattr(
        "zotero_ingest_worker.service_actions.ZoteroMetadataProcessor",
        InvalidMetadataProcessor,
    )

    with pytest.raises(RuntimeError, match="mapping"):
        run_post_action(
            path,
            from_env(load_file=False),
            {},
            _FakeFullRunManager(),
        )


@pytest.mark.parametrize(
    "path, scan_method, drain_method",
    [
        (
            "/api/zotero/metadata/enrich/backlog-scan",
            "metadata_backlog_scan",
            "drain_metadata_queue",
        ),
        (
            "/api/zotero/arxiv-html/backlog-scan",
            "arxiv_html_backlog_scan",
            "drain_arxiv_html_queue",
        ),
        (
            "/api/zotero/full-text/backlog-scan",
            "full_text_backlog_scan",
            "drain_full_text_queue",
        ),
        (
            "/api/zotero/scihub-pdf/backlog-scan",
            "scihub_pdf_backlog_scan",
            "drain_scihub_pdf_queue",
        ),
    ],
)
def test_auto_drain_rejects_non_mapping_nested_result(
    monkeypatch: Any,
    path: str,
    scan_method: str,
    drain_method: str,
) -> None:
    class InvalidMetadataProcessor:
        def __init__(self, _config: Any) -> None:
            pass

        def __getattr__(self, name: str) -> Any:
            if name == scan_method:
                return lambda **_kwargs: {}
            assert name == drain_method
            return lambda **_kwargs: []

    monkeypatch.setattr(
        "zotero_ingest_worker.service_actions.ZoteroMetadataProcessor",
        InvalidMetadataProcessor,
    )

    with pytest.raises(RuntimeError, match="mapping"):
        run_post_action(
            path,
            from_env(load_file=False),
            {"auto_drain": True},
            _FakeFullRunManager(),
        )


def test_status_aliases_are_normalized_before_equivalence_check(
    monkeypatch: Any,
) -> None:
    seen_statuses: list[set[str] | None] = []

    class FakeMetadataProcessor:
        def __init__(self, _config: Any) -> None:
            pass

        def queue(self, **kwargs: Any) -> dict[str, Any]:
            seen_statuses.append(kwargs["statuses"])
            return {"statuses": sorted(kwargs["statuses"] or [])}

    monkeypatch.setattr(
        "zotero_ingest_worker.service_actions.ZoteroMetadataProcessor",
        FakeMetadataProcessor,
    )

    result = run_post_action(
        "/api/zotero/metadata/queue/summary",
        from_env(load_file=False),
        {"status": "failed", "statuses": ["failed_retryable", "failed_final"]},
        _FakeFullRunManager(),
    )

    assert result == {"statuses": ["failed_final", "failed_retryable"]}
    assert seen_statuses == [{"failed_retryable", "failed_final"}]


@pytest.mark.parametrize(
    "invalid_value",
    [
        {"not", "json"},
        float("nan"),
        {1: "non-string-key"},
        ("tuple",),
        "\ud800",
    ],
)
def test_action_result_rejects_nested_non_json_values(invalid_value: Any) -> None:
    class InvalidFullRunManager(_FakeFullRunManager):
        def status(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"value": invalid_value}

    with pytest.raises(RuntimeError, match="JSON"):
        run_post_action(
            "/api/zotero/pipeline/full-run/status",
            from_env(load_file=False),
            {},
            InvalidFullRunManager(),
        )


def test_action_result_rejects_circular_container() -> None:
    circular: dict[str, Any] = {}
    circular["self"] = circular

    class InvalidFullRunManager(_FakeFullRunManager):
        def status(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return circular

    with pytest.raises(RuntimeError, match="circular"):
        run_post_action(
            "/api/zotero/pipeline/full-run/status",
            from_env(load_file=False),
            {},
            InvalidFullRunManager(),
        )


def test_action_result_rejects_excessive_nesting() -> None:
    result: dict[str, Any] = {}
    cursor = result
    for _ in range(MAX_ACTION_RESULT_DEPTH):
        child: dict[str, Any] = {}
        cursor["child"] = child
        cursor = child

    class InvalidFullRunManager(_FakeFullRunManager):
        def status(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return result

    with pytest.raises(RuntimeError, match="nesting"):
        run_post_action(
            "/api/zotero/pipeline/full-run/status",
            from_env(load_file=False),
            {},
            InvalidFullRunManager(),
        )


def test_action_result_accepts_nested_json_value() -> None:
    expected = {"ok": True, "items": [{"value": 1.25}, None, False, "текст"]}

    class ValidFullRunManager(_FakeFullRunManager):
        def status(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return expected

    assert (
        run_post_action(
            "/api/zotero/pipeline/full-run/status",
            from_env(load_file=False),
            {},
            ValidFullRunManager(),
        )
        == expected
    )
