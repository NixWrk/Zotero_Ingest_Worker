from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable

from .full_text_inventory import FullTextAttachmentRecord
from .local_zotero import LocalAttachment, LocalItemMetadata, LocalZoteroStore


TrashAttachment = Callable[..., dict[str, Any]]


def cleanup_source_html_inventory(
    *,
    metadata: LocalItemMetadata,
    inventory: dict[str, object],
    storage_dir: Path,
    trash_attachment: TrashAttachment | None,
    dry_run: bool,
    delete_webdav: bool = False,
) -> dict[str, Any]:
    records = _source_html_records_from_inventory(inventory)
    return cleanup_source_html_records(
        metadata=metadata,
        records=records,
        storage_dir=storage_dir,
        trash_attachment=trash_attachment,
        dry_run=dry_run,
        delete_webdav=delete_webdav,
    )


def cleanup_source_html_records(
    *,
    metadata: LocalItemMetadata,
    records: Iterable[FullTextAttachmentRecord],
    storage_dir: Path,
    trash_attachment: TrashAttachment | None,
    dry_run: bool,
    delete_webdav: bool = False,
) -> dict[str, Any]:
    source_records = [record for record in records if record.is_source_html]
    if not source_records:
        return _cleanup_result(metadata=metadata, dry_run=dry_run, candidates=[], trashed=[])

    keep_key = _source_html_keep_key(source_records)
    candidates = [
        _cleanup_candidate(record, keep_key=keep_key)
        for record in source_records
        if _should_trash_source_html(record, keep_key=keep_key)
    ]
    trashed: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for candidate in candidates:
        if dry_run:
            relay = {"ok": True, "dryRun": True, "wouldTrash": True}
        elif trash_attachment is None:
            relay = {"ok": False, "skipped": True, "reason": "trash_callback_missing"}
        else:
            attachment = _record_attachment(
                metadata=metadata,
                storage_dir=storage_dir,
                record=candidate["record"],
            )
            try:
                relay = trash_attachment(
                    attachment=attachment,
                    dry_run=dry_run,
                    delete_webdav=delete_webdav,
                )
            except Exception as exc:
                relay = {
                    "ok": False,
                    "reason": "trash_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
        if not dry_run and not relay.get("ok"):
            errors.append(
                {
                    "key": candidate["key"],
                    "error": str(relay.get("error") or relay.get("reason") or relay),
                }
            )
        trashed.append({k: v for k, v in candidate.items() if k != "record"} | {"relay": relay})

    result = _cleanup_result(
        metadata=metadata,
        dry_run=dry_run,
        candidates=[{k: v for k, v in candidate.items() if k != "record"} for candidate in candidates],
        trashed=trashed,
    )
    result["keep_key"] = keep_key
    if errors:
        result["ok"] = False
        result["errors"] = errors
    return result


def cleanup_source_html_library(
    *,
    store: LocalZoteroStore,
    trash_attachment: TrashAttachment | None,
    max_items: int,
    dry_run: bool,
    delete_webdav: bool = False,
    collection: str | None = None,
) -> dict[str, Any]:
    scanned = 0
    affected = 0
    total_candidates = 0
    results: list[dict[str, Any]] = []
    for metadata in store.iter_regular_items(max_items=max_items, collection=collection):
        scanned += 1
        inventory = store.full_text_inventory(metadata)
        result = cleanup_source_html_records(
            metadata=metadata,
            records=inventory.attachments,
            storage_dir=store.config.resolved_storage_dir,
            trash_attachment=trash_attachment,
            dry_run=dry_run,
            delete_webdav=delete_webdav,
        )
        if result["candidate_count"]:
            affected += 1
            total_candidates += int(result["candidate_count"])
            results.append(result)
    ok = all(bool(result.get("ok", True)) for result in results)
    return {
        "ok": ok,
        "mode": "source_html_cleanup",
        "library_id": store.library_id,
        "data_dir": str(store.config.zotero_data_dir),
        "dry_run": dry_run,
        "delete_webdav": delete_webdav,
        "collection": collection or None,
        "scanned": scanned,
        "affected_parents": affected,
        "candidate_count": total_candidates,
        "results": results,
    }


def _source_html_records_from_inventory(inventory: dict[str, object]) -> list[FullTextAttachmentRecord]:
    raw = inventory.get("attachments")
    if not isinstance(raw, list):
        return []
    records: list[FullTextAttachmentRecord] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        record = FullTextAttachmentRecord(
            key=str(item.get("key") or ""),
            content_type=str(item.get("content_type") or ""),
            path=str(item.get("path") or ""),
            title=str(item.get("title") or ""),
            file_path=str(item.get("file_path") or ""),
            exists=_optional_bool(item.get("exists")),
        )
        if record.key:
            records.append(record)
    return records


def _source_html_keep_key(records: list[FullTextAttachmentRecord]) -> str | None:
    existing = [record for record in records if record.exists is not False]
    if not existing:
        return None
    return max(existing, key=_source_html_rank).key


def _source_html_rank(record: FullTextAttachmentRecord) -> tuple[int, int, str]:
    path = Path(record.file_path) if record.file_path else None
    size = 0
    mtime = 0
    if path is not None:
        try:
            stat = path.stat()
            size = int(stat.st_size)
            mtime = int(stat.st_mtime_ns)
        except OSError:
            pass
    return (size, mtime, record.key)


def _should_trash_source_html(record: FullTextAttachmentRecord, *, keep_key: str | None) -> bool:
    if record.exists is False:
        return True
    return keep_key is not None and record.key != keep_key


def _cleanup_candidate(record: FullTextAttachmentRecord, *, keep_key: str | None) -> dict[str, Any]:
    return {
        "key": record.key,
        "reason": "missing_file" if record.exists is False else "duplicate_source_html",
        "keep_key": keep_key,
        "title": record.title,
        "path": record.path,
        "file_path": record.file_path,
        "exists": record.exists,
        "record": record,
    }


def _record_attachment(
    *,
    metadata: LocalItemMetadata,
    storage_dir: Path,
    record: FullTextAttachmentRecord,
) -> LocalAttachment:
    return LocalAttachment(
        library_id=metadata.library_id,
        data_dir=metadata.data_dir,
        storage_dir=storage_dir,
        key=record.key,
        item_id=None,
        parent_item_id=metadata.item_id,
        date_modified=None,
        link_mode=None,
        content_type=record.content_type or "text/html",
        zotero_path=record.path,
        file_path=Path(record.file_path) if record.file_path else storage_dir / record.key,
        parent_key=metadata.key,
    )


def _cleanup_result(
    *,
    metadata: LocalItemMetadata,
    dry_run: bool,
    candidates: list[dict[str, Any]],
    trashed: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "ok": True,
        "parent_key": str(getattr(metadata, "key", "") or ""),
        "library_id": str(getattr(metadata, "library_id", "") or ""),
        "title": str(getattr(metadata, "title", "") or ""),
        "dry_run": dry_run,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "trashed": trashed,
    }


def _optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    return bool(value)
