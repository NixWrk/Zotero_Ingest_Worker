from __future__ import annotations

import math
from typing import Any

from .config import (
    MAX_CONFIG_PATH_CHARS,
    MAX_LIBRARY_BINDINGS,
    MAX_OPERATION_ITEMS,
    METADATA_POLICIES,
    WorkerConfig,
    apply_request_overrides,
)
from .full_run import FullRunManager
from .full_run_options import FullRunOptions, MAX_FULL_RUN_DRAIN_ITEMS
from .metadata_jobs import (
    METADATA_JOB_ARXIV_HTML,
    METADATA_JOB_ENRICH,
    METADATA_JOB_FULL_TEXT,
    METADATA_JOB_RESEARCHGATE_PDF,
    METADATA_JOB_SCIHUB_PDF,
)
from .metadata_processor import ZoteroMetadataProcessor
from .state import MAX_FULL_RUN_EVENT_LIMIT
from .worker_roles import (
    FULLTEXT_JOB_TYPES,
    METADATA_JOB_TYPES,
    POST_ACTION_PATHS,
    ROLE_ALL,
    ensure_role_allows_action,
)


class ActionRequestError(ValueError):
    """A request payload failed validation before action execution."""


__all__ = ["ActionRequestError", "POST_ACTION_PATHS", "run_post_action"]
MAX_ACTION_RESULT_DEPTH = 64
MAX_DRAIN_ITEMS = MAX_FULL_RUN_DRAIN_ITEMS
MAX_QUEUE_PAGE_ITEMS = 50_000
MAX_QUEUE_OFFSET = MAX_OPERATION_ITEMS
MAX_FILTER_ITEMS = 1_024
MAX_FILTER_ITEM_CHARS = 256
MAX_PARENT_FILTER_LIBRARIES = MAX_LIBRARY_BINDINGS
MAX_PARENT_FILTER_KEYS = 50_000
MAX_PARENT_FILTER_KEY_CHARS = 256
MAX_ACTION_IDENTIFIER_CHARS = 256
MAX_ACTION_COLLECTION_CHARS = 1_024
METADATA_QUEUE_STATUSES = frozenset(
    {
        "cancelled",
        "failed_final",
        "failed_retryable",
        "queued",
        "running",
        "skipped",
        "succeeded",
    }
)
_ALL_METADATA_JOB_TYPES = METADATA_JOB_TYPES | FULLTEXT_JOB_TYPES
_BACKLOG_PATHS = frozenset(
    {
        "/api/zotero/metadata/enrich/backlog-scan",
        "/api/zotero/arxiv-html/backlog-scan",
        "/api/zotero/full-text/backlog-scan",
        "/api/zotero/scihub-pdf/backlog-scan",
    }
)
_EXPECTED_DRAIN_JOB_TYPES = {
    "/api/zotero/metadata/enrich/queue/drain": METADATA_JOB_ENRICH,
    "/api/zotero/arxiv-html/queue/drain": METADATA_JOB_ARXIV_HTML,
    "/api/zotero/full-text/queue/drain": METADATA_JOB_FULL_TEXT,
    "/api/zotero/researchgate-pdf/queue/drain": METADATA_JOB_RESEARCHGATE_PDF,
    "/api/zotero/scihub-pdf/queue/drain": METADATA_JOB_SCIHUB_PDF,
}
_STRING_FIELD_LIMITS = {
    "collection": MAX_ACTION_COLLECTION_CHARS,
    "data_dir": MAX_CONFIG_PATH_CHARS,
    "job_id": MAX_ACTION_IDENTIFIER_CHARS,
    "job_type": MAX_ACTION_IDENTIFIER_CHARS,
    "library_id": MAX_ACTION_IDENTIFIER_CHARS,
    "policy": MAX_ACTION_IDENTIFIER_CHARS,
    "run_id": MAX_ACTION_IDENTIFIER_CHARS,
    "type": MAX_ACTION_IDENTIFIER_CHARS,
}
_BOOLEAN_PAYLOAD_FIELDS = frozenset(
    {
        "arxiv_html_backlog_intake",
        "arxiv_html_drain",
        "auto_drain",
        "confirm",
        "delete_webdav",
        "dry_run",
        "force",
        "full_text_backlog_intake",
        "full_text_drain",
        "metadata_backlog_intake",
        "metadata_drain",
        "require_relay",
        "researchgate_pdf_drain",
        "reset_attempts",
        "retry_failed",
        "scihub_pdf_backlog_intake",
        "scihub_pdf_drain",
        "stop_when_idle",
    }
)
_NUMERIC_PAYLOAD_FIELDS = frozenset(
    {
        "drain_limit",
        "event_limit",
        "idle_cycles_to_complete",
        "intake_interval_seconds",
        "limit",
        "max_items",
        "max_workers",
        "offset",
        "poll_seconds",
        "queue_limit",
        "workers",
    }
)
_STRING_PAYLOAD_FIELDS = frozenset(
    {
        "collection",
        "data_dir",
        "job_id",
        "job_type",
        "library_id",
        "policy",
        "run_id",
        "type",
    }
)
_STRING_FILTER_PAYLOAD_FIELDS = frozenset({"library_ids", "status", "statuses"})
_BACKLOG_COMMON_FIELDS = frozenset(
    {
        "auto_drain",
        "collection",
        "data_dir",
        "drain_limit",
        "dry_run",
        "force",
        "library_id",
        "limit",
        "max_items",
        "max_workers",
        "only_parent_keys_by_library",
        "require_relay",
        "workers",
        "zotero",
    }
)
_DRAIN_COMMON_FIELDS = frozenset(
    {
        "dry_run",
        "job_type",
        "limit",
        "max_workers",
        "require_relay",
        "type",
        "workers",
        "zotero",
    }
)
_ALLOWED_PAYLOAD_FIELDS: dict[str, frozenset[str]] = {
    "/api/zotero/pipeline/full-run/start": frozenset(
        {
            "arxiv_html_backlog_intake",
            "arxiv_html_drain",
            "drain_limit",
            "dry_run",
            "force",
            "full_text_backlog_intake",
            "full_text_drain",
            "idle_cycles_to_complete",
            "intake_interval_seconds",
            "limit",
            "max_items",
            "metadata_backlog_intake",
            "metadata_drain",
            "poll_seconds",
            "queue_limit",
            "require_relay",
            "researchgate_pdf_drain",
            "scihub_pdf_backlog_intake",
            "scihub_pdf_drain",
            "stop_when_idle",
        }
    ),
    "/api/zotero/pipeline/full-run/status": frozenset({"event_limit", "run_id"}),
    "/api/zotero/pipeline/full-run/stop": frozenset({"run_id"}),
    "/api/zotero/metadata/queue/summary": frozenset(
        {
            "job_type",
            "library_id",
            "library_ids",
            "limit",
            "offset",
            "status",
            "statuses",
            "type",
        }
    ),
    "/api/zotero/metadata/enrich/backlog-scan": _BACKLOG_COMMON_FIELDS | {"policy"},
    "/api/zotero/arxiv-html/backlog-scan": _BACKLOG_COMMON_FIELDS,
    "/api/zotero/full-text/backlog-scan": _BACKLOG_COMMON_FIELDS,
    "/api/zotero/scihub-pdf/backlog-scan": _BACKLOG_COMMON_FIELDS,
    "/api/zotero/metadata/enrich/queue/drain": _DRAIN_COMMON_FIELDS | {"policy"},
    "/api/zotero/arxiv-html/queue/drain": _DRAIN_COMMON_FIELDS,
    "/api/zotero/full-text/queue/drain": _DRAIN_COMMON_FIELDS,
    "/api/zotero/researchgate-pdf/queue/drain": _DRAIN_COMMON_FIELDS,
    "/api/zotero/scihub-pdf/queue/drain": _DRAIN_COMMON_FIELDS,
    "/api/zotero/source-html/cleanup": frozenset(
        {
            "collection",
            "confirm",
            "data_dir",
            "delete_webdav",
            "dry_run",
            "library_id",
            "limit",
            "max_items",
            "zotero",
        }
    ),
    "/api/zotero/metadata/queue/retry": frozenset({"job_id", "reset_attempts"}),
    "/api/zotero/metadata/queue/cancel": frozenset({"job_id"}),
}


def run_post_action(
    path: str,
    base_config: WorkerConfig,
    payload: dict[str, Any],
    full_run_manager: FullRunManager,
    *,
    role: str = ROLE_ALL,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ActionRequestError("Request payload must be a JSON object.")
    ensure_role_allows_action(role, path, payload)
    try:
        _validate_boolean_payload(payload)
        _validate_numeric_payload(payload)
        _validate_string_payload(payload)
        _validate_route_payload(path, payload)
        parent_keys_by_library = _parent_keys_by_library(
            payload.get("only_parent_keys_by_library")
        )
        config = apply_request_overrides(base_config, payload)
    except ValueError as exc:
        raise ActionRequestError(str(exc)) from exc

    if path == "/api/zotero/pipeline/full-run/start":
        return _action_result(full_run_manager.start(payload), action=path)
    if path == "/api/zotero/pipeline/full-run/status":
        return _action_result(
            full_run_manager.status(
                _optional_str(payload.get("run_id"), field="run_id"),
                event_limit=_event_limit_value(payload.get("event_limit")),
            ),
            action=path,
        )
    if path == "/api/zotero/pipeline/full-run/stop":
        return _action_result(
            full_run_manager.stop(_optional_str(payload.get("run_id"), field="run_id")),
            action=path,
        )

    metadata_processor = ZoteroMetadataProcessor(config)

    if path == "/api/zotero/metadata/queue/summary":
        return _action_result(
            metadata_processor.queue(
                job_type=_queue_job_type(payload),
                statuses=_status_filter(payload),
                limit=_int_value(payload.get("limit"), 100),
                offset=_int_value(payload.get("offset"), 0),
                library_ids=_library_id_filter(payload),
            ),
            action=path,
        )
    if path == "/api/zotero/metadata/enrich/backlog-scan":
        result = _action_result(
            metadata_processor.metadata_backlog_scan(
                max_items=_optional_int(payload.get("max_items")),
                limit=_optional_int(payload.get("limit")),
                force=bool(payload.get("force", False)),
                library_id=_optional_str(payload.get("library_id")),
                data_dir=_optional_str(payload.get("data_dir")),
                collection=_optional_str(payload.get("collection")),
                only_parent_keys_by_library=parent_keys_by_library,
            ),
            action=path,
        )
        if bool(payload.get("auto_drain", False)):
            result["drain"] = _action_result(
                metadata_processor.drain_metadata_queue(
                    limit=_int_value(payload.get("drain_limit"), 1),
                    dry_run=bool(payload.get("dry_run", False)),
                    require_relay=bool(payload.get("require_relay", True)),
                    policy=_optional_str(payload.get("policy")),
                ),
                action=f"{path} auto-drain",
            )
        return result
    if path == "/api/zotero/metadata/enrich/queue/drain":
        return _action_result(
            metadata_processor.drain_metadata_queue(
                limit=_int_value(payload.get("limit"), 1),
                dry_run=bool(payload.get("dry_run", False)),
                require_relay=bool(payload.get("require_relay", True)),
                policy=_optional_str(payload.get("policy")),
            ),
            action=path,
        )
    if path == "/api/zotero/arxiv-html/backlog-scan":
        result = _action_result(
            metadata_processor.arxiv_html_backlog_scan(
                max_items=_optional_int(payload.get("max_items")),
                limit=_optional_int(payload.get("limit")),
                force=bool(payload.get("force", False)),
                library_id=_optional_str(payload.get("library_id")),
                data_dir=_optional_str(payload.get("data_dir")),
                collection=_optional_str(payload.get("collection")),
                only_parent_keys_by_library=parent_keys_by_library,
            ),
            action=path,
        )
        if bool(payload.get("auto_drain", False)):
            result["drain"] = _action_result(
                metadata_processor.drain_arxiv_html_queue(
                    limit=_int_value(payload.get("drain_limit"), 1),
                    dry_run=bool(payload.get("dry_run", False)),
                    require_relay=bool(payload.get("require_relay", True)),
                ),
                action=f"{path} auto-drain",
            )
        return result
    if path == "/api/zotero/arxiv-html/queue/drain":
        return _action_result(
            metadata_processor.drain_arxiv_html_queue(
                limit=_int_value(payload.get("limit"), 1),
                dry_run=bool(payload.get("dry_run", False)),
                require_relay=bool(payload.get("require_relay", True)),
            ),
            action=path,
        )
    if path == "/api/zotero/full-text/backlog-scan":
        result = _action_result(
            metadata_processor.full_text_backlog_scan(
                max_items=_optional_int(payload.get("max_items")),
                limit=_optional_int(payload.get("limit")),
                force=bool(payload.get("force", False)),
                library_id=_optional_str(payload.get("library_id")),
                data_dir=_optional_str(payload.get("data_dir")),
                collection=_optional_str(payload.get("collection")),
                only_parent_keys_by_library=parent_keys_by_library,
            ),
            action=path,
        )
        if bool(payload.get("auto_drain", False)):
            result["drain"] = _action_result(
                metadata_processor.drain_full_text_queue(
                    limit=_int_value(payload.get("drain_limit"), 1),
                    dry_run=bool(payload.get("dry_run", False)),
                    require_relay=bool(payload.get("require_relay", True)),
                ),
                action=f"{path} auto-drain",
            )
        return result
    if path == "/api/zotero/full-text/queue/drain":
        return _action_result(
            metadata_processor.drain_full_text_queue(
                limit=_int_value(payload.get("limit"), 1),
                dry_run=bool(payload.get("dry_run", False)),
                require_relay=bool(payload.get("require_relay", True)),
            ),
            action=path,
        )
    if path == "/api/zotero/source-html/cleanup":
        return _action_result(
            metadata_processor.source_html_cleanup(
                max_items=_optional_int(payload.get("max_items")),
                limit=_optional_int(payload.get("limit")),
                dry_run=bool(payload.get("dry_run", True)),
                confirm=bool(payload.get("confirm", False)),
                delete_webdav=bool(payload.get("delete_webdav", False)),
                library_id=_optional_str(payload.get("library_id")),
                data_dir=_optional_str(payload.get("data_dir")),
                collection=_optional_str(payload.get("collection")),
            ),
            action=path,
        )
    if path == "/api/zotero/scihub-pdf/backlog-scan":
        result = _action_result(
            metadata_processor.scihub_pdf_backlog_scan(
                max_items=_optional_int(payload.get("max_items")),
                limit=_optional_int(payload.get("limit")),
                force=bool(payload.get("force", False)),
                library_id=_optional_str(payload.get("library_id")),
                data_dir=_optional_str(payload.get("data_dir")),
                collection=_optional_str(payload.get("collection")),
                only_parent_keys_by_library=parent_keys_by_library,
            ),
            action=path,
        )
        if bool(payload.get("auto_drain", False)):
            result["drain"] = _action_result(
                metadata_processor.drain_scihub_pdf_queue(
                    limit=_int_value(payload.get("drain_limit"), 1),
                    dry_run=bool(payload.get("dry_run", False)),
                    require_relay=bool(payload.get("require_relay", True)),
                ),
                action=f"{path} auto-drain",
            )
        return result
    if path == "/api/zotero/researchgate-pdf/queue/drain":
        return _action_result(
            metadata_processor.drain_researchgate_pdf_queue(
                limit=_int_value(payload.get("limit"), 1),
                dry_run=bool(payload.get("dry_run", False)),
                require_relay=bool(payload.get("require_relay", True)),
            ),
            action=path,
        )
    if path == "/api/zotero/scihub-pdf/queue/drain":
        return _action_result(
            metadata_processor.drain_scihub_pdf_queue(
                limit=_int_value(payload.get("limit"), 1),
                dry_run=bool(payload.get("dry_run", False)),
                require_relay=bool(payload.get("require_relay", True)),
            ),
            action=path,
        )
    if path == "/api/zotero/metadata/queue/retry":
        job_id = _required_job_id(payload, path=path)
        job = metadata_processor.state.retry_metadata_job(
            job_id,
            reset_attempts=bool(payload.get("reset_attempts", False)),
        )
        return _metadata_transition_response(
            job,
            expected_status="queued",
            action="retry",
        )
    if path == "/api/zotero/metadata/queue/cancel":
        job_id = _required_job_id(payload, path=path)
        job = metadata_processor.state.cancel_metadata_job(job_id)
        return _metadata_transition_response(
            job,
            expected_status="cancelled",
            action="cancel",
        )
    raise ValueError(f"Unsupported POST action: {path}")


def _validate_route_payload(path: str, payload: dict[str, Any]) -> None:
    _validate_allowed_payload_fields(path, payload)
    if path == "/api/zotero/pipeline/full-run/start":
        FullRunOptions.from_payload(payload)
    elif path == "/api/zotero/pipeline/full-run/status":
        _event_limit_value(payload.get("event_limit"))
    elif path in {
        "/api/zotero/metadata/queue/retry",
        "/api/zotero/metadata/queue/cancel",
    }:
        _required_job_id(payload, path=path)
    elif path == "/api/zotero/metadata/queue/summary":
        _bounded_payload_integer(
            payload, "limit", minimum=0, maximum=MAX_QUEUE_PAGE_ITEMS
        )
        _bounded_payload_integer(payload, "offset", minimum=0, maximum=MAX_QUEUE_OFFSET)
        _queue_job_type(payload)
        _status_filter(payload)
        _library_id_filter(payload)
    elif path in _BACKLOG_PATHS:
        _bounded_payload_integer(
            payload,
            "drain_limit",
            minimum=1,
            maximum=MAX_DRAIN_ITEMS,
        )
    elif path in _EXPECTED_DRAIN_JOB_TYPES:
        _bounded_payload_integer(
            payload,
            "limit",
            minimum=1,
            maximum=MAX_DRAIN_ITEMS,
        )
        selected_job_type = _queue_job_type(payload)
        expected_job_type = _EXPECTED_DRAIN_JOB_TYPES[path]
        if selected_job_type is not None and selected_job_type != expected_job_type:
            raise ValueError(f"job_type for {path} must be {expected_job_type!r}.")

    policy = _optional_str(payload.get("policy"), field="policy")
    if policy is not None and policy not in METADATA_POLICIES:
        raise ValueError(
            f"policy must be one of: {', '.join(sorted(METADATA_POLICIES))}."
        )


def _validate_allowed_payload_fields(path: str, payload: dict[str, Any]) -> None:
    allowed = _ALLOWED_PAYLOAD_FIELDS.get(path)
    if allowed is None:
        raise ValueError(f"Unsupported POST action: {path}")
    unknown = sorted(set(payload) - allowed)
    if not unknown:
        return
    noun = "field" if len(unknown) == 1 else "fields"
    raise ValueError(f"Unsupported request {noun} for {path}: {', '.join(unknown)}.")


def _action_result(value: object, *, action: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{action} must return a JSON object mapping.")
    _validate_action_result_json(value, action=action)
    return value


def _validate_action_result_json(value: object, *, action: str) -> None:
    active_containers: set[int] = set()
    pending: list[tuple[object, int, bool]] = [(value, 1, False)]
    while pending:
        current, depth, exiting = pending.pop()
        if exiting:
            active_containers.remove(id(current))
            continue
        if depth > MAX_ACTION_RESULT_DEPTH:
            raise RuntimeError(f"{action} returned JSON with excessive nesting.")

        if isinstance(current, (dict, list)):
            identity = id(current)
            if identity in active_containers:
                raise RuntimeError(f"{action} returned a circular JSON container.")
            active_containers.add(identity)
            pending.append((current, depth, True))
            if isinstance(current, dict):
                for key, nested in reversed(list(current.items())):
                    if not isinstance(key, str):
                        raise RuntimeError(
                            f"{action} returned a JSON object with a non-string key."
                        )
                    _validate_action_result_text(key, action=action)
                    pending.append((nested, depth + 1, False))
            else:
                for nested in reversed(current):
                    pending.append((nested, depth + 1, False))
            continue

        if current is None or isinstance(current, (bool, int)):
            continue
        if isinstance(current, str):
            _validate_action_result_text(current, action=action)
            continue
        if isinstance(current, float):
            if math.isfinite(current):
                continue
            raise RuntimeError(f"{action} returned a non-finite JSON number.")
        raise RuntimeError(
            f"{action} returned a value that is not JSON-compatible: "
            f"{type(current).__name__}."
        )


def _validate_action_result_text(value: str, *, action: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RuntimeError(
            f"{action} returned text that is not valid UTF-8 JSON content."
        ) from exc


def _bounded_payload_integer(
    payload: dict[str, Any],
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> None:
    value = payload.get(field)
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be a JSON integer.")
    if not minimum <= value <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}.")


def _queue_job_type(payload: dict[str, Any]) -> str | None:
    job_type = _optional_str(payload.get("job_type"), field="job_type")
    alias = _optional_str(payload.get("type"), field="type")
    if job_type is not None and alias is not None and job_type != alias:
        raise ValueError("job_type and type must identify the same queue.")
    selected = job_type or alias
    if selected is not None and selected not in _ALL_METADATA_JOB_TYPES:
        raise ValueError(
            "job_type must be one of: "
            + ", ".join(sorted(_ALL_METADATA_JOB_TYPES))
            + "."
        )
    return selected


def _required_job_id(payload: dict[str, Any], *, path: str) -> str:
    job_id = _optional_str(payload.get("job_id"), field="job_id")
    if not job_id:
        action = path.rsplit("/", 1)[-1]
        raise ValueError(f"metadata queue/{action} requires job_id.")
    return job_id


def _metadata_transition_response(
    job: object,
    *,
    expected_status: str,
    action: str,
) -> dict[str, Any]:
    if not isinstance(job, dict):
        raise RuntimeError(f"metadata queue {action} returned an invalid job result.")
    return {
        "ok": bool(job) and job.get("status") == expected_status,
        "job": job,
    }


def _validate_boolean_payload(payload: dict[str, Any]) -> None:
    for field in sorted(_BOOLEAN_PAYLOAD_FIELDS.intersection(payload)):
        if not isinstance(payload[field], bool):
            raise ValueError(f"{field} must be a JSON boolean.")


def _validate_numeric_payload(payload: dict[str, Any]) -> None:
    for field in sorted(_NUMERIC_PAYLOAD_FIELDS.intersection(payload)):
        value = payload[field]
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int)
        ):
            raise ValueError(f"{field} must be a JSON integer.")


def _validate_string_payload(payload: dict[str, Any]) -> None:
    for field in sorted(_STRING_PAYLOAD_FIELDS.intersection(payload)):
        value = payload[field]
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError(f"{field} must be a JSON string or null.")
        _validated_action_text(
            value,
            field=field,
            maximum=_STRING_FIELD_LIMITS[field],
        )
    for field in sorted(_STRING_FILTER_PAYLOAD_FIELDS.intersection(payload)):
        _string_filter(payload[field], field=field)


def _validated_action_text(value: str, *, field: str, maximum: int) -> str:
    if len(value) > maximum:
        raise ValueError(f"{field} must be at most {maximum} characters.")
    if any(not character.isprintable() for character in value):
        raise ValueError(f"{field} must contain only printable characters.")
    return value.strip()


def _optional_str(value: object, *, field: str = "value") -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a JSON string or null.")
    text = _validated_action_text(
        value,
        field=field,
        maximum=_STRING_FIELD_LIMITS.get(field, MAX_ACTION_IDENTIFIER_CHARS),
    )
    return text or None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Expected a JSON integer.")
    if value < 0:
        raise ValueError("Expected a non-negative JSON integer.")
    return value


def _int_value(value: object, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Expected a JSON integer.")
    return value


def _event_limit_value(value: object) -> int:
    if value is None:
        return 50
    if type(value) is not int:
        raise ValueError("event_limit must be a JSON integer.")
    if not 0 <= value <= MAX_FULL_RUN_EVENT_LIMIT:
        raise ValueError(
            f"event_limit must be between 0 and {MAX_FULL_RUN_EVENT_LIMIT}."
        )
    return value


def _status_filter(payload: dict[str, Any]) -> set[str] | None:
    plural = _expanded_status_aliases(
        _string_filter(payload.get("statuses"), field="statuses")
    )
    singular = _expanded_status_aliases(
        _string_filter(payload.get("status"), field="status")
    )
    if plural is not None and singular is not None and plural != singular:
        raise ValueError("status and statuses must select the same states.")
    result = plural or singular
    unsupported = (result or set()) - METADATA_QUEUE_STATUSES
    if unsupported:
        raise ValueError(
            "status contains unsupported metadata queue states: "
            + ", ".join(sorted(unsupported))
            + "."
        )
    return result


def _expanded_status_aliases(statuses: set[str] | None) -> set[str] | None:
    if statuses is None or "failed" not in statuses:
        return statuses
    return (statuses - {"failed"}) | {
        "failed_final",
        "failed_retryable",
    }


def _library_id_filter(payload: dict[str, Any]) -> set[str] | None:
    plural = _string_filter(payload.get("library_ids"), field="library_ids")
    singular = _string_filter(payload.get("library_id"), field="library_id")
    if plural is not None and singular is not None and plural != singular:
        raise ValueError("library_id and library_ids must select the same libraries.")
    return plural or singular


def _string_filter(value: object, *, field: str) -> set[str] | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, list):
        if any(not isinstance(part, str) for part in value):
            raise ValueError(f"{field} must be a JSON string or array of strings.")
        values = value
    else:
        raise ValueError(f"{field} must be a JSON string or array of strings.")
    if len(values) > MAX_FILTER_ITEMS:
        raise ValueError(f"{field} allows at most {MAX_FILTER_ITEMS} entries.")
    result: set[str] = set()
    for part in values:
        normalized = _validated_action_text(
            part,
            field=f"{field} entry",
            maximum=MAX_FILTER_ITEM_CHARS,
        )
        if normalized:
            result.add(normalized)
    return result or None


def _parent_keys_by_library(value: object) -> dict[str, list[str]] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("only_parent_keys_by_library must be a JSON object.")
    if len(value) > MAX_PARENT_FILTER_LIBRARIES:
        raise ValueError(
            "only_parent_keys_by_library allows at most "
            f"{MAX_PARENT_FILTER_LIBRARIES} libraries."
        )

    result: dict[str, list[str]] = {}
    total_keys = 0
    for raw_library_id, raw_keys in value.items():
        if not isinstance(raw_library_id, str):
            raise ValueError(
                "only_parent_keys_by_library library ids must be non-empty JSON strings."
            )
        library_id = _validated_action_text(
            raw_library_id,
            field="only_parent_keys_by_library library id",
            maximum=MAX_ACTION_IDENTIFIER_CHARS,
        )
        if not library_id:
            raise ValueError(
                "only_parent_keys_by_library library ids must be non-empty JSON strings."
            )
        if library_id in result:
            raise ValueError(
                "only_parent_keys_by_library contains duplicate normalized library ids."
            )
        if not isinstance(raw_keys, list):
            raise ValueError(
                f"only_parent_keys_by_library[{library_id!r}] must be a JSON array."
            )
        total_keys += len(raw_keys)
        if total_keys > MAX_PARENT_FILTER_KEYS:
            raise ValueError(
                "only_parent_keys_by_library allows at most "
                f"{MAX_PARENT_FILTER_KEYS} parent keys."
            )
        parent_keys: set[str] = set()
        for raw_key in raw_keys:
            if not isinstance(raw_key, str):
                raise ValueError(
                    "only_parent_keys_by_library entries must be non-empty JSON strings."
                )
            parent_key = _validated_action_text(
                raw_key,
                field="only_parent_keys_by_library parent key",
                maximum=MAX_PARENT_FILTER_KEY_CHARS,
            )
            if not parent_key:
                raise ValueError(
                    "only_parent_keys_by_library entries must be non-empty JSON strings."
                )
            parent_keys.add(parent_key)
        result[library_id] = sorted(parent_keys)
    return result
