from __future__ import annotations

from typing import Any

from .full_text_inventory import should_skip_full_text_scan
from .local_zotero import LocalZoteroStore
from .metadata_jobs import (
    METADATA_JOB_FULL_TEXT,
    METADATA_JOB_SCIHUB_PDF,
)
from .metadata_processor_helpers import _enqueue_item_result


def attachment_backlog_scan(
    processor: Any,
    *,
    job_type: str,
    max_items: int | None,
    limit: int | None,
    force: bool,
    library_id: str | None,
    data_dir: str | None,
    collection: str | None,
) -> dict[str, Any]:
    processor.config.validate_for_scan()
    scanned = 0
    queued = 0
    skipped = 0
    results: list[dict[str, Any]] = []
    effective_limit = _effective_limit(limit)

    for library_config in processor._library_configs(library_id=library_id, data_dir=data_dir):
        zotero = LocalZoteroStore(library_config)
        scan_limit = max_items if max_items is not None else max(
            zotero.count_sqlite_pdf_attachments(),
            1_000_000,
        )
        attachments = (
            zotero.iter_collection_pdf_attachments(
                collection=collection,
                max_items=scan_limit,
            )
            if collection
            else zotero.iter_pdf_attachments(max_items=scan_limit)
        )
        for attachment in attachments:
            scanned += 1
            result = processor._enqueue_attachment(
                zotero=zotero,
                attachment=attachment,
                job_type=job_type,
                force=force,
                reason=f"{job_type}_backlog_scan",
            )
            results.append(result)
            job = result.get("job") or {}
            if job.get("created") and job.get("status") == "queued":
                queued += 1
            else:
                skipped += 1
            if _limit_reached(effective_limit, queued):
                break
        if _limit_reached(effective_limit, queued):
            break

    return {
        "ok": True,
        "mode": f"{job_type}_backlog_scan",
        "job_type": job_type,
        "scanned": scanned,
        "queued": queued,
        "skipped": skipped,
        "queue": processor.state.metadata_queue_summary(job_type=job_type),
        "results": results,
    }


def full_text_backlog_scan(
    processor: Any,
    *,
    max_items: int | None,
    limit: int | None,
    force: bool,
    library_id: str | None,
    data_dir: str | None,
    collection: str | None,
) -> dict[str, Any]:
    processor.config.validate_for_scan()
    scanned = 0
    queued = 0
    skipped = 0
    results: list[dict[str, Any]] = []
    effective_limit = _effective_limit(limit)

    for library_config in processor._library_configs(library_id=library_id, data_dir=data_dir):
        zotero = LocalZoteroStore(library_config)
        scan_limit = max_items if max_items is not None else 1_000_000
        for metadata in zotero.iter_regular_items(
            max_items=scan_limit,
            collection=collection,
        ):
            scanned += 1
            inventory = zotero.item_full_text_inventory(metadata)
            if should_skip_full_text_scan(inventory) and not force:
                result = _enqueue_item_result(
                    metadata,
                    "html_exists",
                    message="Parent item already has source HTML and PDF attachments.",
                    inventory=inventory,
                )
            else:
                result = processor._enqueue_parent_full_text_item(
                    zotero=zotero,
                    metadata=metadata,
                    inventory=inventory,
                    force=force,
                    reason="full_text_backlog_scan",
                )
            results.append(result)
            job = result.get("job") or {}
            if job.get("created") and job.get("status") == "queued":
                queued += 1
            else:
                skipped += 1
            if _limit_reached(effective_limit, queued):
                break
        if _limit_reached(effective_limit, queued):
            break

    return {
        "ok": True,
        "mode": "full_text_backlog_scan",
        "job_type": METADATA_JOB_FULL_TEXT,
        "scanned": scanned,
        "queued": queued,
        "skipped": skipped,
        "queue": processor.state.metadata_queue_summary(job_type=METADATA_JOB_FULL_TEXT),
        "results": results,
    }


def scihub_pdf_backlog_scan(
    processor: Any,
    *,
    max_items: int | None,
    limit: int | None,
    force: bool,
    library_id: str | None,
    data_dir: str | None,
    collection: str | None,
) -> dict[str, Any]:
    processor.config.validate_for_scan()
    scanned = 0
    queued = 0
    skipped = 0
    results: list[dict[str, Any]] = []
    effective_limit = _effective_limit(limit)

    for library_config in processor._library_configs(library_id=library_id, data_dir=data_dir):
        zotero = LocalZoteroStore(library_config)
        scan_limit = max_items if max_items is not None else 1_000_000
        for metadata in zotero.iter_regular_items(
            max_items=scan_limit,
            collection=collection,
        ):
            scanned += 1
            inventory = zotero.item_full_text_inventory(metadata)
            if inventory.get("has_pdf") and not force:
                result = _enqueue_item_result(
                    metadata,
                    "pdf_exists",
                    message="Parent item already has a PDF attachment.",
                    inventory=inventory,
                )
                skipped += 1
            else:
                result = processor._enqueue_scihub_pdf_jobs_for_item(
                    metadata=metadata,
                    inventory=inventory,
                    reason="scihub_pdf_backlog_scan",
                    force=force,
                )
                queued += int(result.get("queued") or 0)
                if not result.get("queued"):
                    skipped += 1
            results.append(result)
            if _limit_reached(effective_limit, queued):
                break
        if _limit_reached(effective_limit, queued):
            break

    return {
        "ok": True,
        "mode": "scihub_pdf_backlog_scan",
        "job_type": METADATA_JOB_SCIHUB_PDF,
        "scanned": scanned,
        "queued": queued,
        "skipped": skipped,
        "queue": processor.state.metadata_queue_summary(job_type=METADATA_JOB_SCIHUB_PDF),
        "results": results,
    }


def _effective_limit(limit: int | None) -> int | None:
    return limit if limit is not None and limit > 0 else None


def _limit_reached(limit: int | None, queued: int) -> bool:
    return limit is not None and queued >= limit
