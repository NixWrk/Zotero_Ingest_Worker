from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .full_text_inventory import should_skip_full_text_scan
from .local_zotero import LocalZoteroStore
from .local_zotero_paths import path_library_id_for_data_dir
from .metadata_jobs import (
    METADATA_JOB_FULL_TEXT,
    METADATA_JOB_SCIHUB_PDF,
)
from .metadata_processor_helpers import _enqueue_item_result


_MAX_BACKLOG_RESULT_ITEMS = 1_000


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
    scan_limit = _effective_limit(max_items, field="max_items")
    effective_limit = _effective_limit(limit, field="limit")
    parent_filters = _validated_parent_filters(only_parent_keys_by_library)
    bindings = _zfr_library_bindings() if parent_filters is not None else ()
    processor.config.validate_for_scan()
    scanned = 0
    queued = 0
    skipped = 0
    results: list[dict[str, Any]] = []

    for library_config in processor._library_configs(
        library_id=library_id, data_dir=data_dir
    ):
        if _limit_reached(effective_limit, queued) or _limit_reached(
            scan_limit, scanned
        ):
            break
        remaining_scan = _remaining_limit(scan_limit, scanned)
        allowed_parent_keys = _allowed_parent_keys_for_library(
            library_config,
            parent_filters,
            bindings,
        )
        if allowed_parent_keys is not None and not allowed_parent_keys:
            continue
        zotero = LocalZoteroStore(library_config)
        local_scan_limit = None if allowed_parent_keys is not None else remaining_scan
        attachments = (
            zotero.iter_collection_pdf_attachments(
                collection=collection,
                max_items=local_scan_limit,
            )
            if collection
            else zotero.iter_pdf_attachments(max_items=local_scan_limit)
        )
        for attachment in attachments:
            parent_key = str(attachment.parent_key or attachment.key or "").strip()
            if (
                allowed_parent_keys is not None
                and parent_key not in allowed_parent_keys
            ):
                continue
            scanned += 1
            result = processor._enqueue_attachment(
                zotero=zotero,
                attachment=attachment,
                job_type=job_type,
                force=force,
                reason=f"{job_type}_backlog_scan",
            )
            result = _validated_result(result, context=f"{job_type} backlog enqueue")
            _append_result(results, result)
            if _job_result_was_queued(result):
                queued += 1
            else:
                skipped += 1
            if _limit_reached(effective_limit, queued) or _limit_reached(
                scan_limit, scanned
            ):
                break
        if _limit_reached(effective_limit, queued) or _limit_reached(
            scan_limit, scanned
        ):
            break

    return {
        "ok": True,
        "mode": f"{job_type}_backlog_scan",
        "job_type": job_type,
        "scanned": scanned,
        "queued": queued,
        "skipped": skipped,
        "queue": processor.state.metadata_queue_summary(job_type=job_type),
        **_result_window(scanned=scanned, results=results),
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
    scan_limit = _effective_limit(max_items, field="max_items")
    effective_limit = _effective_limit(limit, field="limit")
    parent_filters = _validated_parent_filters(only_parent_keys_by_library)
    bindings = _zfr_library_bindings() if parent_filters is not None else ()
    processor.config.validate_for_scan()
    scanned = 0
    queued = 0
    skipped = 0
    results: list[dict[str, Any]] = []

    for library_config in processor._library_configs(
        library_id=library_id, data_dir=data_dir
    ):
        if _limit_reached(effective_limit, queued) or _limit_reached(
            scan_limit, scanned
        ):
            break
        remaining_scan = _remaining_limit(scan_limit, scanned)
        allowed_parent_keys = _allowed_parent_keys_for_library(
            library_config,
            parent_filters,
            bindings,
        )
        if allowed_parent_keys is not None and not allowed_parent_keys:
            continue
        zotero = LocalZoteroStore(library_config)
        for metadata in zotero.iter_regular_items(
            max_items=remaining_scan,
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
            result = _validated_result(result, context="full-text backlog enqueue")
            _append_result(results, result)
            if _job_result_was_queued(result):
                queued += 1
            else:
                skipped += 1
            if _limit_reached(effective_limit, queued) or _limit_reached(
                scan_limit, scanned
            ):
                break
        if _limit_reached(effective_limit, queued) or _limit_reached(
            scan_limit, scanned
        ):
            break

    return {
        "ok": True,
        "mode": "full_text_backlog_scan",
        "job_type": METADATA_JOB_FULL_TEXT,
        "scanned": scanned,
        "queued": queued,
        "skipped": skipped,
        "queue": processor.state.metadata_queue_summary(
            job_type=METADATA_JOB_FULL_TEXT
        ),
        **_result_window(scanned=scanned, results=results),
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
    scan_limit = _effective_limit(max_items, field="max_items")
    effective_limit = _effective_limit(limit, field="limit")
    parent_filters = _validated_parent_filters(only_parent_keys_by_library)
    bindings = _zfr_library_bindings() if parent_filters is not None else ()
    processor.config.validate_for_scan()
    scanned = 0
    queued = 0
    skipped = 0
    results: list[dict[str, Any]] = []

    for library_config in processor._library_configs(
        library_id=library_id, data_dir=data_dir
    ):
        if _limit_reached(effective_limit, queued) or _limit_reached(
            scan_limit, scanned
        ):
            break
        remaining_scan = _remaining_limit(scan_limit, scanned)
        allowed_parent_keys = _allowed_parent_keys_for_library(
            library_config,
            parent_filters,
            bindings,
        )
        if allowed_parent_keys is not None and not allowed_parent_keys:
            continue
        zotero = LocalZoteroStore(library_config)
        for metadata in zotero.iter_regular_items(
            max_items=remaining_scan,
            collection=collection,
            only_keys=allowed_parent_keys,
        ):
            scanned += 1
            inventory = zotero.item_full_text_inventory(metadata)
            if inventory.get("has_pdf") is True and not force:
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
                result = _validated_result(result, context="Sci-Hub backlog enqueue")
                queued_count = _scihub_queued_count(result)
                queued += queued_count
                if queued_count == 0:
                    skipped += 1
            result = _validated_result(result, context="Sci-Hub backlog result")
            _append_result(results, result)
            if _limit_reached(effective_limit, queued) or _limit_reached(
                scan_limit, scanned
            ):
                break
        if _limit_reached(effective_limit, queued) or _limit_reached(
            scan_limit, scanned
        ):
            break

    return {
        "ok": True,
        "mode": "scihub_pdf_backlog_scan",
        "job_type": METADATA_JOB_SCIHUB_PDF,
        "scanned": scanned,
        "queued": queued,
        "skipped": skipped,
        "queue": processor.state.metadata_queue_summary(
            job_type=METADATA_JOB_SCIHUB_PDF
        ),
        **_result_window(scanned=scanned, results=results),
    }


def _effective_limit(value: object, *, field: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise ValueError(f"{field} must be a JSON integer or null")
    if value < 0:
        raise ValueError(f"{field} must be non-negative")
    return value or None


def _limit_reached(limit: int | None, current: int) -> bool:
    return limit is not None and current >= limit


def _remaining_limit(limit: int | None, current: int) -> int | None:
    return None if limit is None else max(0, limit - current)


def _append_result(results: list[dict[str, Any]], result: dict[str, Any]) -> None:
    if len(results) < _MAX_BACKLOG_RESULT_ITEMS:
        results.append(result)


def _result_window(
    *,
    scanned: int,
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    omitted = max(0, scanned - len(results))
    return {
        "results": results,
        "results_truncated": omitted > 0,
        "omitted_results": omitted,
    }


def _validated_result(value: object, *, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{context} returned a non-object result")
    return value


def _job_result_was_queued(result: dict[str, Any]) -> bool:
    job = result.get("job")
    if job is None:
        return False
    if not isinstance(job, dict):
        raise RuntimeError("backlog enqueue job must be an object or null")
    created = job.get("created")
    if type(created) is not bool:
        raise RuntimeError("backlog enqueue job.created must be a boolean")
    status = job.get("status")
    if not isinstance(status, str) or not status.strip():
        raise RuntimeError("backlog enqueue job.status must be a non-empty string")
    return created and status == "queued"


def _scihub_queued_count(result: dict[str, Any]) -> int:
    value = result.get("queued")
    if type(value) is not int or not 0 <= value <= 1:
        raise RuntimeError("Sci-Hub backlog queued must be exactly 0 or 1")
    return value


def _validated_parent_filters(
    value: object,
) -> dict[str, list[str]] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("only_parent_keys_by_library must be a JSON object")
    normalized: dict[str, list[str]] = {}
    for raw_library_id, raw_keys in value.items():
        if not isinstance(raw_library_id, str) or not raw_library_id.strip():
            raise ValueError("parent filter library ids must be non-empty strings")
        library_id = raw_library_id.strip()
        if library_id in normalized:
            raise ValueError("parent filter contains duplicate normalized library ids")
        if not isinstance(raw_keys, list):
            raise ValueError(f"parent keys for {library_id!r} must be a JSON array")
        keys: set[str] = set()
        for raw_key in raw_keys:
            if not isinstance(raw_key, str) or not raw_key.strip():
                raise ValueError("parent keys must be non-empty strings")
            keys.add(raw_key.strip())
        normalized[library_id] = sorted(keys)
    return normalized


def _allowed_parent_keys_for_library(
    library_config: Any,
    filters: dict[str, list[str]] | None,
    bindings: tuple[dict[str, Any], ...],
) -> set[str] | None:
    if filters is None:
        return None
    aliases = _library_aliases_for_config(library_config, bindings)
    allowed: set[str] = set()
    for alias in aliases:
        raw_keys = filters.get(alias)
        if raw_keys is None:
            continue
        allowed.update(raw_keys)
    return allowed


def _library_aliases_for_config(
    library_config: Any,
    bindings: tuple[dict[str, Any], ...],
) -> set[str]:
    data_dir = Path(getattr(library_config, "zotero_data_dir"))
    aliases = {path_library_id_for_data_dir(data_dir)}
    for binding in bindings:
        if not _binding_matches_data_dir(library_config, binding, data_dir):
            continue
        for key in ("libraryId", "zoteroLibraryId"):
            value = binding.get(key)
            library_id = value.strip() if isinstance(value, str) else ""
            if library_id:
                aliases.add(library_id)
    return aliases


def _zfr_library_bindings() -> tuple[dict[str, Any], ...]:
    value = os.environ.get("ZFR_LIBRARY_BINDINGS", "").strip()
    if not value:
        return ()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("ZFR_LIBRARY_BINDINGS must contain valid JSON") from exc
    if not isinstance(parsed, list):
        raise ValueError("ZFR_LIBRARY_BINDINGS must be a JSON array")
    bindings: list[dict[str, Any]] = []
    string_fields = ("libraryId", "zoteroLibraryId", "dataDir", "hostDataDir")
    for index, item in enumerate(parsed):
        if not isinstance(item, dict):
            raise ValueError(f"ZFR_LIBRARY_BINDINGS[{index}] must be a JSON object")
        for field in string_fields:
            field_value = item.get(field)
            if field_value is not None and not isinstance(field_value, str):
                raise ValueError(
                    f"ZFR_LIBRARY_BINDINGS[{index}].{field} must be a string or null"
                )
        bindings.append(item)
    return tuple(bindings)


def _binding_matches_data_dir(
    library_config: Any,
    binding: dict[str, Any],
    data_dir: Path,
) -> bool:
    for key in ("dataDir", "hostDataDir"):
        value = binding.get(key)
        raw = value.strip() if isinstance(value, str) else ""
        if not raw:
            continue
        for candidate in _path_candidates_from_binding(library_config, raw):
            if _same_path(candidate, data_dir):
                return True
    return False


def _path_candidates_from_binding(library_config: Any, raw: str) -> list[Path]:
    candidates = [Path(raw)]
    for source_prefix, target_prefix in (
        getattr(library_config, "zotero_path_prefix_map", ()) or ()
    ):
        normalized_raw = raw.replace("\\", "/")
        normalized_source = str(source_prefix).replace("\\", "/").rstrip("/")
        folded_raw = normalized_raw.casefold()
        folded_source = normalized_source.casefold()
        if folded_raw == folded_source:
            rest = ""
        elif folded_raw.startswith(f"{folded_source}/"):
            rest = normalized_raw[len(normalized_source) :].lstrip("/")
        else:
            continue
        candidates.append(
            Path(str(target_prefix), *([part for part in rest.split("/") if part]))
        )
    return candidates


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.expanduser().resolve() == right.expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return _normalize_path(left) == _normalize_path(right)


def _normalize_path(path: Path) -> str:
    return str(path).replace("\\", "/").rstrip("/").casefold()
