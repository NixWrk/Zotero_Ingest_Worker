from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .full_text_inventory import should_skip_full_text_scan
from .local_zotero import LocalZoteroStore
from .local_zotero_paths import library_id_for_data_dir
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
    only_parent_keys_by_library: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    processor.config.validate_for_scan()
    scanned = 0
    queued = 0
    skipped = 0
    results: list[dict[str, Any]] = []
    effective_limit = _effective_limit(limit)

    for library_config in processor._library_configs(library_id=library_id, data_dir=data_dir):
        allowed_parent_keys = _allowed_parent_keys_for_library(
            library_config,
            only_parent_keys_by_library,
        )
        if allowed_parent_keys is not None and not allowed_parent_keys:
            continue
        zotero = LocalZoteroStore(library_config)
        scan_limit = max_items if max_items is not None else None
        attachments = (
            zotero.iter_collection_pdf_attachments(
                collection=collection,
                max_items=scan_limit,
            )
            if collection
            else zotero.iter_pdf_attachments(max_items=scan_limit)
        )
        for attachment in attachments:
            parent_key = str(attachment.parent_key or attachment.key or "").strip()
            if allowed_parent_keys is not None and parent_key not in allowed_parent_keys:
                continue
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
    only_parent_keys_by_library: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    processor.config.validate_for_scan()
    scanned = 0
    queued = 0
    skipped = 0
    results: list[dict[str, Any]] = []
    effective_limit = _effective_limit(limit)

    for library_config in processor._library_configs(library_id=library_id, data_dir=data_dir):
        allowed_parent_keys = _allowed_parent_keys_for_library(
            library_config,
            only_parent_keys_by_library,
        )
        if allowed_parent_keys is not None and not allowed_parent_keys:
            continue
        zotero = LocalZoteroStore(library_config)
        scan_limit = max_items if max_items is not None else None
        for metadata in zotero.iter_regular_items(
            max_items=scan_limit,
            collection=collection,
            only_keys=allowed_parent_keys,
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
    only_parent_keys_by_library: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    processor.config.validate_for_scan()
    scanned = 0
    queued = 0
    skipped = 0
    results: list[dict[str, Any]] = []
    effective_limit = _effective_limit(limit)

    for library_config in processor._library_configs(library_id=library_id, data_dir=data_dir):
        allowed_parent_keys = _allowed_parent_keys_for_library(
            library_config,
            only_parent_keys_by_library,
        )
        if allowed_parent_keys is not None and not allowed_parent_keys:
            continue
        zotero = LocalZoteroStore(library_config)
        scan_limit = max_items if max_items is not None else None
        for metadata in zotero.iter_regular_items(
            max_items=scan_limit,
            collection=collection,
            only_keys=allowed_parent_keys,
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


def _allowed_parent_keys_for_library(
    library_config: Any,
    filters: dict[str, list[str]] | None,
) -> set[str] | None:
    if filters is None:
        return None
    aliases = _library_aliases_for_config(library_config)
    allowed: set[str] = set()
    for alias in aliases:
        raw_keys = filters.get(alias)
        if not isinstance(raw_keys, list):
            continue
        allowed.update(str(key).strip() for key in raw_keys if str(key).strip())
    return allowed


def _library_aliases_for_config(library_config: Any) -> set[str]:
    data_dir = Path(getattr(library_config, "zotero_data_dir"))
    aliases = {library_id_for_data_dir(data_dir)}
    for binding in _zfr_library_bindings():
        if not isinstance(binding, dict):
            continue
        if not _binding_matches_data_dir(library_config, binding, data_dir):
            continue
        for key in ("libraryId", "zoteroLibraryId"):
            library_id = str(binding.get(key) or "").strip()
            if library_id:
                aliases.add(library_id)
    return aliases


def _zfr_library_bindings() -> list[dict[str, Any]]:
    value = os.environ.get("ZFR_LIBRARY_BINDINGS", "").strip()
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _binding_matches_data_dir(
    library_config: Any,
    binding: dict[str, Any],
    data_dir: Path,
) -> bool:
    for key in ("dataDir", "hostDataDir"):
        raw = str(binding.get(key) or "").strip()
        if not raw:
            continue
        for candidate in _path_candidates_from_binding(library_config, raw):
            if _same_path(candidate, data_dir):
                return True
    return False


def _path_candidates_from_binding(library_config: Any, raw: str) -> list[Path]:
    candidates = [Path(raw)]
    for source_prefix, target_prefix in getattr(library_config, "zotero_path_prefix_map", ()) or ():
        normalized_raw = raw.replace("\\", "/")
        normalized_source = str(source_prefix).replace("\\", "/").rstrip("/")
        if not normalized_raw.lower().startswith(normalized_source.lower()):
            continue
        rest = normalized_raw[len(normalized_source) :].lstrip("/")
        candidates.append(Path(str(target_prefix), *([part for part in rest.split("/") if part])))
    return candidates


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.expanduser().resolve() == right.expanduser().resolve()
    except OSError:
        return _normalize_path(left) == _normalize_path(right)


def _normalize_path(path: Path) -> str:
    return str(path).replace("\\", "/").rstrip("/").casefold()
