from __future__ import annotations

from typing import Any

from .config import WorkerConfig, apply_request_overrides
from .full_run import FullRunManager
from .metadata_processor import ZoteroMetadataProcessor


POST_ACTION_PATHS = frozenset(
    {
        "/api/zotero/metadata/queue/summary",
        "/api/zotero/metadata/enrich/backlog-scan",
        "/api/zotero/metadata/enrich/queue/drain",
        "/api/zotero/arxiv-html/backlog-scan",
        "/api/zotero/arxiv-html/queue/drain",
        "/api/zotero/full-text/backlog-scan",
        "/api/zotero/full-text/queue/drain",
        "/api/zotero/scihub-pdf/backlog-scan",
        "/api/zotero/researchgate-pdf/queue/drain",
        "/api/zotero/scihub-pdf/queue/drain",
        "/api/zotero/metadata/queue/retry",
        "/api/zotero/metadata/queue/cancel",
        "/api/zotero/pipeline/full-run/start",
        "/api/zotero/pipeline/full-run/status",
        "/api/zotero/pipeline/full-run/stop",
    }
)


def run_post_action(
    path: str,
    base_config: WorkerConfig,
    payload: dict[str, Any],
    full_run_manager: FullRunManager,
) -> dict[str, Any]:
    config = apply_request_overrides(base_config, payload)
    metadata_processor = ZoteroMetadataProcessor(config)

    if path == "/api/zotero/metadata/queue/summary":
        return metadata_processor.queue(
            job_type=_optional_str(payload.get("job_type") or payload.get("type")),
            statuses=_status_filter(payload),
            limit=int(payload.get("limit") or 100),
        )
    if path == "/api/zotero/metadata/enrich/backlog-scan":
        result = metadata_processor.metadata_backlog_scan(
            max_items=_optional_int(payload.get("max_items")),
            limit=_optional_int(payload.get("limit")),
            force=bool(payload.get("force", False)),
            library_id=_optional_str(payload.get("library_id")),
            data_dir=_optional_str(payload.get("data_dir")),
            collection=_optional_str(payload.get("collection")),
        )
        if bool(payload.get("auto_drain", False)):
            result["drain"] = metadata_processor.drain_metadata_queue(
                limit=_int_value(payload.get("drain_limit"), 1),
                dry_run=bool(payload.get("dry_run", False)),
                require_relay=bool(payload.get("require_relay", True)),
                policy=_optional_str(payload.get("policy")),
            )
        return result
    if path == "/api/zotero/metadata/enrich/queue/drain":
        return metadata_processor.drain_metadata_queue(
            limit=_int_value(payload.get("limit"), 1),
            dry_run=bool(payload.get("dry_run", False)),
            require_relay=bool(payload.get("require_relay", True)),
            policy=_optional_str(payload.get("policy")),
        )
    if path == "/api/zotero/arxiv-html/backlog-scan":
        result = metadata_processor.arxiv_html_backlog_scan(
            max_items=_optional_int(payload.get("max_items")),
            limit=_optional_int(payload.get("limit")),
            force=bool(payload.get("force", False)),
            library_id=_optional_str(payload.get("library_id")),
            data_dir=_optional_str(payload.get("data_dir")),
            collection=_optional_str(payload.get("collection")),
        )
        if bool(payload.get("auto_drain", False)):
            result["drain"] = metadata_processor.drain_arxiv_html_queue(
                limit=_int_value(payload.get("drain_limit"), 1),
                dry_run=bool(payload.get("dry_run", False)),
                require_relay=bool(payload.get("require_relay", True)),
            )
        return result
    if path == "/api/zotero/arxiv-html/queue/drain":
        return metadata_processor.drain_arxiv_html_queue(
            limit=_int_value(payload.get("limit"), 1),
            dry_run=bool(payload.get("dry_run", False)),
            require_relay=bool(payload.get("require_relay", True)),
        )
    if path == "/api/zotero/full-text/backlog-scan":
        result = metadata_processor.full_text_backlog_scan(
            max_items=_optional_int(payload.get("max_items")),
            limit=_optional_int(payload.get("limit")),
            force=bool(payload.get("force", False)),
            library_id=_optional_str(payload.get("library_id")),
            data_dir=_optional_str(payload.get("data_dir")),
            collection=_optional_str(payload.get("collection")),
        )
        if bool(payload.get("auto_drain", False)):
            result["drain"] = metadata_processor.drain_full_text_queue(
                limit=_int_value(payload.get("drain_limit"), 1),
                dry_run=bool(payload.get("dry_run", False)),
            )
        return result
    if path == "/api/zotero/full-text/queue/drain":
        return metadata_processor.drain_full_text_queue(
            limit=_int_value(payload.get("limit"), 1),
            dry_run=bool(payload.get("dry_run", False)),
        )
    if path == "/api/zotero/scihub-pdf/backlog-scan":
        result = metadata_processor.scihub_pdf_backlog_scan(
            max_items=_optional_int(payload.get("max_items")),
            limit=_optional_int(payload.get("limit")),
            force=bool(payload.get("force", False)),
            library_id=_optional_str(payload.get("library_id")),
            data_dir=_optional_str(payload.get("data_dir")),
            collection=_optional_str(payload.get("collection")),
        )
        if bool(payload.get("auto_drain", False)):
            result["drain"] = metadata_processor.drain_scihub_pdf_queue(
                limit=_int_value(payload.get("drain_limit"), 1),
                dry_run=bool(payload.get("dry_run", False)),
                require_relay=bool(payload.get("require_relay", True)),
            )
        return result
    if path == "/api/zotero/researchgate-pdf/queue/drain":
        return metadata_processor.drain_researchgate_pdf_queue(
            limit=_int_value(payload.get("limit"), 1),
            dry_run=bool(payload.get("dry_run", False)),
            require_relay=bool(payload.get("require_relay", True)),
        )
    if path == "/api/zotero/scihub-pdf/queue/drain":
        return metadata_processor.drain_scihub_pdf_queue(
            limit=_int_value(payload.get("limit"), 1),
            dry_run=bool(payload.get("dry_run", False)),
            require_relay=bool(payload.get("require_relay", True)),
        )
    if path == "/api/zotero/metadata/queue/retry":
        job_id = _optional_str(payload.get("job_id"))
        if not job_id:
            raise ValueError("metadata queue/retry requires job_id.")
        return {"ok": True, "job": metadata_processor.state.retry_metadata_job(job_id)}
    if path == "/api/zotero/metadata/queue/cancel":
        job_id = _optional_str(payload.get("job_id"))
        if not job_id:
            raise ValueError("metadata queue/cancel requires job_id.")
        return {"ok": True, "job": metadata_processor.state.cancel_metadata_job(job_id)}
    if path == "/api/zotero/pipeline/full-run/start":
        return full_run_manager.start(payload)
    if path == "/api/zotero/pipeline/full-run/status":
        return full_run_manager.status(
            _optional_str(payload.get("run_id")),
            event_limit=_int_value(payload.get("event_limit"), 50),
        )
    if path == "/api/zotero/pipeline/full-run/stop":
        return full_run_manager.stop(_optional_str(payload.get("run_id")))
    raise ValueError(f"Unsupported POST action: {path}")


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


def _int_value(value: object, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


def _status_filter(payload: dict[str, Any]) -> set[str] | None:
    raw = payload.get("statuses") or payload.get("status")
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        values = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, list):
        values = [str(part).strip() for part in raw]
    else:
        values = [str(raw).strip()]
    result = {value for value in values if value}
    return result or None
