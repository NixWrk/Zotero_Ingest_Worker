from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .local_zotero import LocalAttachment, LocalItemMetadata


def replace_original_file(*, source_path: Path, output_pdf: Path) -> None:
    temp_path = source_path.with_name(f".{source_path.name}.ocr-tmp")
    shutil.copy2(output_pdf, temp_path)
    os.replace(temp_path, source_path)


def write_new_attachment_local_copy(
    *,
    attachment: LocalAttachment,
    source_path: Path,
    relay_result: dict[str, Any],
    backups_root: Path,
) -> dict[str, Any]:
    new_key = str(relay_result.get("newAttachmentKey") or "").strip()
    if not new_key:
        raise RuntimeError(
            "zotero-file-relay OCR replacement did not return newAttachmentKey."
        )

    filename = str(relay_result.get("filename") or source_path.name).strip()
    safe_filename = Path(filename).name
    if not safe_filename:
        raise RuntimeError(
            "zotero-file-relay OCR replacement did not return a usable filename."
        )

    target_dir = attachment.storage_dir / new_key
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / safe_filename
    temp_path = target_dir / f".{safe_filename}.ocr-tmp"

    backup_path: Path | None = None
    if target_path.exists():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_dir = backups_root / f"{attachment.library_id}_{new_key}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"{stamp}_{safe_filename}"
        shutil.copy2(target_path, backup_path)

    shutil.copy2(source_path, temp_path)
    os.replace(temp_path, target_path)
    return {
        "ok": True,
        "newAttachmentKey": new_key,
        "path": str(target_path),
        "backupPath": str(backup_path) if backup_path else None,
    }


def sync_local_zotero_storage_metadata(
    *,
    attachment: LocalAttachment,
    relay_result: dict[str, Any],
) -> dict[str, Any]:
    webdav = relay_result.get("webDav")
    if not isinstance(webdav, dict):
        raise RuntimeError("Relay result is missing WebDAV metadata for local Zotero sync.")
    storage_hash = str(webdav.get("md5") or "").strip()
    storage_mtime = webdav.get("mtime")
    if not storage_hash or storage_mtime is None:
        raise RuntimeError("Relay WebDAV result is missing md5/mtime.")
    metadata_patch = webdav.get("metadataPatch")
    if not isinstance(metadata_patch, dict) or not metadata_patch.get("ok"):
        raise RuntimeError(
            "Relay result is missing a successful Zotero Web API metadata patch. "
            "Headless replacement needs Zotero API md5/mtime to be updated."
        )
    zotero_version = metadata_patch.get("newVersion")
    if zotero_version is None:
        raise RuntimeError("Relay metadata patch did not return the new Zotero item version.")
    if attachment.item_id is None:
        raise RuntimeError(f"Attachment has no local Zotero item id: {attachment.key}")

    sqlite_path = attachment.data_dir / "zotero.sqlite"
    connection = sqlite3.connect(str(sqlite_path), timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        before_row = connection.execute(
            """
            select
              i.itemID,
              i.libraryID,
              i.key,
              i.version,
              i.synced,
              i.dateModified,
              i.clientDateModified,
              ia.storageHash,
              ia.storageModTime,
              ia.syncState
            from itemAttachments ia
            join items i on i.itemID = ia.itemID
            where ia.itemID = ?
            """,
            (attachment.item_id,),
        ).fetchone()
        if before_row is None:
            raise RuntimeError(f"Attachment metadata row was not found: {attachment.key}")
        before_sync_cache = connection.execute(
            """
            select version, data
            from syncCache
            where libraryID = ? and key = ? and syncObjectTypeID = 3
            """,
            (before_row["libraryID"], attachment.key),
        ).fetchone()
        patched_sync_cache_data = patched_sync_cache_json(
            before_sync_cache["data"] if before_sync_cache is not None else None,
            attachment_key=attachment.key,
            version=int(zotero_version),
            storage_hash=storage_hash,
            storage_mtime=int(storage_mtime),
        )
        connection.execute(
            """
            update itemAttachments
            set storageHash = ?, storageModTime = ?, syncState = 2
            where itemID = ?
            """,
            (storage_hash, int(storage_mtime), attachment.item_id),
        )
        connection.execute(
            """
            update items
            set version = ?, synced = 1
            where itemID = ?
            """,
            (int(zotero_version), attachment.item_id),
        )
        sync_cache_updated = 0
        if before_sync_cache is not None and patched_sync_cache_data is not None:
            cursor = connection.execute(
                """
                update syncCache
                set version = ?, data = ?
                where libraryID = ? and key = ? and syncObjectTypeID = 3
                """,
                (
                    int(zotero_version),
                    patched_sync_cache_data,
                    before_row["libraryID"],
                    attachment.key,
                ),
            )
            sync_cache_updated = cursor.rowcount
        connection.commit()
        after_row = connection.execute(
            """
            select
              i.itemID,
              i.libraryID,
              i.key,
              i.version,
              i.synced,
              i.dateModified,
              i.clientDateModified,
              ia.storageHash,
              ia.storageModTime,
              ia.syncState
            from itemAttachments ia
            join items i on i.itemID = ia.itemID
            where ia.itemID = ?
            """,
            (attachment.item_id,),
        ).fetchone()
        after_sync_cache = connection.execute(
            """
            select version, data
            from syncCache
            where libraryID = ? and key = ? and syncObjectTypeID = 3
            """,
            (before_row["libraryID"], attachment.key),
        ).fetchone()
    finally:
        connection.close()

    return {
        "ok": True,
        "sqlite_path": str(sqlite_path),
        "item_id": attachment.item_id,
        "zotero_version": int(zotero_version),
        "storage_hash": storage_hash,
        "storage_mtime": int(storage_mtime),
        "sync_cache_updated": sync_cache_updated,
        "before": dict(before_row),
        "after": dict(after_row) if after_row is not None else None,
        "sync_cache": {
            "before_version": (
                before_sync_cache["version"] if before_sync_cache is not None else None
            ),
            "after_version": (
                after_sync_cache["version"] if after_sync_cache is not None else None
            ),
            "updated": bool(sync_cache_updated),
        },
    }


def sync_parent_attachment_local(
    *,
    metadata: LocalItemMetadata,
    attachment: LocalAttachment,
    filename: str,
    title: str,
    content_type: str,
    relay_result: dict[str, Any],
) -> dict[str, Any]:
    new_key = str(
        relay_result.get("newAttachmentKey") or relay_result.get("attachmentKey") or ""
    ).strip()
    if not new_key:
        return {"ok": False, "reason": "missing_attachment_key"}

    sqlite_path = metadata.data_dir / "zotero.sqlite"
    if not sqlite_path.exists():
        return {
            "ok": True,
            "updated": False,
            "reason": "sqlite_missing",
            "attachmentKey": new_key,
            "sqlite_path": str(sqlite_path),
        }

    webdav = relay_result.get("webDav")
    webdav_data = webdav if isinstance(webdav, dict) else {}
    metadata_patch = webdav_data.get("metadataPatch")
    metadata_patch_data = metadata_patch if isinstance(metadata_patch, dict) else {}
    zotero_version = _optional_int(
        relay_result.get("newAttachmentVersion") or metadata_patch_data.get("newVersion")
    )
    storage_hash = str(webdav_data.get("md5") or "").strip()
    storage_mtime = _optional_int(webdav_data.get("mtime"))
    if zotero_version is None:
        return {"ok": False, "reason": "missing_zotero_version", "attachmentKey": new_key}

    now = datetime.now(UTC)
    sqlite_timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
    api_timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    safe_filename = Path(filename).name
    connection = sqlite3.connect(str(sqlite_path), timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        existing = connection.execute(
            "select itemID from items where key = ? limit 1",
            (new_key,),
        ).fetchone()
        if existing is not None:
            return {
                "ok": True,
                "updated": False,
                "reason": "attachment_already_exists",
                "attachmentKey": new_key,
                "item_id": int(existing["itemID"]),
            }

        parent = connection.execute(
            "select itemID, libraryID from items where key = ? limit 1",
            (metadata.key,),
        ).fetchone()
        if parent is None:
            return {
                "ok": False,
                "reason": "parent_missing",
                "attachmentKey": new_key,
                "parentKey": metadata.key,
            }

        item_type = connection.execute(
            "select itemTypeID from itemTypes where typeName = 'attachment' limit 1",
        ).fetchone()
        if item_type is None:
            return {"ok": False, "reason": "attachment_item_type_missing"}

        cursor = connection.execute(
            """
            insert into items (
              itemTypeID,
              dateAdded,
              dateModified,
              clientDateModified,
              libraryID,
              key,
              version,
              synced
            )
            values (?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                int(item_type["itemTypeID"]),
                sqlite_timestamp,
                sqlite_timestamp,
                sqlite_timestamp,
                int(parent["libraryID"]),
                new_key,
                zotero_version,
            ),
        )
        item_id = _required_lastrowid(cursor)
        connection.execute(
            """
            insert into itemAttachments (
              itemID,
              parentItemID,
              linkMode,
              contentType,
              charsetID,
              path,
              syncState,
              storageModTime,
              storageHash,
              lastProcessedModificationTime,
              lastRead
            )
            values (?, ?, 0, ?, null, ?, 2, ?, ?, null, null)
            """,
            (
                item_id,
                int(parent["itemID"]),
                content_type,
                f"storage:{safe_filename}",
                storage_mtime,
                storage_hash or None,
            ),
        )

        if title:
            field_id = _field_id(connection, "title")
            if field_id is not None:
                value_id = _item_data_value_id(connection, title)
                _upsert_item_data(connection, item_id=item_id, field_id=field_id, value_id=value_id)

        sync_payload = {
            "key": new_key,
            "version": zotero_version,
            "data": {
                "key": new_key,
                "version": zotero_version,
                "parentItem": metadata.key,
                "itemType": "attachment",
                "linkMode": "imported_file",
                "title": title,
                "accessDate": "",
                "url": "",
                "note": "",
                "contentType": content_type,
                "charset": "",
                "filename": safe_filename,
                "md5": storage_hash,
                "mtime": storage_mtime,
                "tags": [],
                "relations": {},
                "dateAdded": api_timestamp,
                "dateModified": api_timestamp,
            },
        }
        connection.execute(
            """
            insert or replace into syncCache (
              libraryID,
              key,
              syncObjectTypeID,
              version,
              data
            )
            values (?, ?, 3, ?, ?)
            """,
            (
                int(parent["libraryID"]),
                new_key,
                zotero_version,
                json.dumps(sync_payload, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    return {
        "ok": True,
        "updated": True,
        "sqlite_path": str(sqlite_path),
        "attachmentKey": new_key,
        "parentKey": metadata.key,
        "item_id": item_id,
        "zotero_version": zotero_version,
        "storage_hash": storage_hash or None,
        "storage_mtime": storage_mtime,
    }


def sync_ensured_parent_local(
    *,
    attachment: LocalAttachment,
    relay_result: dict[str, Any],
) -> dict[str, Any]:
    parent_key = str(relay_result.get("parentItemKey") or "").strip()
    if not parent_key:
        return {"ok": False, "reason": "missing_parent_key"}

    sqlite_path = attachment.data_dir / "zotero.sqlite"
    if not sqlite_path.exists():
        return {
            "ok": True,
            "updated": False,
            "reason": "sqlite_missing",
            "parentKey": parent_key,
            "sqlite_path": str(sqlite_path),
        }

    parent_created = relay_result.get("parentCreated")
    parent_data = parent_created if isinstance(parent_created, dict) else {}
    pdf_parent_patch = relay_result.get("pdfParentPatch")
    pdf_patch_data = pdf_parent_patch if isinstance(pdf_parent_patch, dict) else {}
    parent_title = str(parent_data.get("title") or Path(attachment.filename).stem or "Untitled PDF")
    parent_type = str(parent_data.get("itemType") or "document")
    parent_version = _optional_int(parent_data.get("version"))
    pdf_version = _optional_int(pdf_patch_data.get("newVersion"))
    collection_keys = [
        str(value).strip()
        for value in (parent_data.get("collections") or [])
        if str(value).strip()
    ]

    connection = sqlite3.connect(str(sqlite_path), timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        attachment_item_id = attachment.item_id
        attachment_inserted = False
        if attachment_item_id is None:
            row = connection.execute(
                "select itemID from items where key = ? limit 1",
                (attachment.key,),
            ).fetchone()
            attachment_item_id = int(row["itemID"]) if row is not None else None

        parent_row = connection.execute(
            "select itemID from items where key = ? limit 1",
            (parent_key,),
        ).fetchone()
        parent_inserted = False
        if parent_row is None:
            parent_item_id = _insert_parent_item(
                connection,
                source_item_id=attachment_item_id,
                parent_key=parent_key,
                parent_type=parent_type,
                parent_title=parent_title,
                parent_version=parent_version,
            )
            parent_inserted = True
        else:
            parent_item_id = int(parent_row["itemID"])

        if attachment_item_id is None:
            attachment_item_id = _insert_attachment_item(
                connection,
                attachment=attachment,
                parent_item_id=parent_item_id,
                version=pdf_version,
            )
            attachment_inserted = True
        else:
            connection.execute(
                "update itemAttachments set parentItemID = ? where itemID = ?",
                (parent_item_id, attachment_item_id),
            )
        item_columns = _table_columns(connection, "items")
        if pdf_version is not None and "version" in item_columns:
            assignments = ["version = ?"]
            values: list[object] = [pdf_version]
            if "synced" in item_columns:
                assignments.append("synced = 1")
            connection.execute(
                f"update items set {', '.join(assignments)} where itemID = ?",
                (*values, attachment_item_id),
            )

        field_id = _field_id(connection, "title")
        if field_id is not None:
            value_id = _item_data_value_id(connection, parent_title)
            _upsert_item_data(
                connection,
                item_id=parent_item_id,
                field_id=field_id,
                value_id=value_id,
            )

        collection_sync = _sync_parent_collections(
            connection,
            parent_item_id=parent_item_id,
            attachment_item_id=attachment_item_id,
            collection_keys=collection_keys,
        )
        connection.commit()
    finally:
        connection.close()

    return {
        "ok": True,
        "updated": True,
        "sqlite_path": str(sqlite_path),
        "attachmentKey": attachment.key,
        "attachmentItemId": attachment_item_id,
        "parentKey": parent_key,
        "parentItemId": parent_item_id,
        "parentInserted": parent_inserted,
        "attachmentInserted": attachment_inserted,
        "parentVersion": parent_version,
        "pdfVersion": pdf_version,
        "collections": collection_sync,
    }


def sync_parent_metadata_local(
    *,
    metadata: LocalItemMetadata,
    fields: dict[str, str],
    relay_result: dict[str, Any],
) -> dict[str, Any]:
    applied_fields = {
        str(field).strip()
        for field in (relay_result.get("appliedFields") or [])
        if str(field).strip()
    }
    if not applied_fields:
        return {
            "ok": True,
            "updated": False,
            "reason": "no_applied_fields",
            "item_key": metadata.key,
        }

    sqlite_path = metadata.data_dir / "zotero.sqlite"
    connection = sqlite3.connect(str(sqlite_path), timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        before = _read_item_field_values(connection, metadata.item_id)
        updated_fields: list[str] = []
        skipped_fields: dict[str, str] = {}
        for field_name in sorted(applied_fields):
            if field_name not in fields:
                skipped_fields[field_name] = "field_value_missing"
                continue
            field_id = _field_id(connection, field_name)
            if field_id is None:
                skipped_fields[field_name] = "unknown_local_field"
                continue
            value_id = _item_data_value_id(connection, str(fields[field_name]))
            _upsert_item_data(
                connection,
                item_id=metadata.item_id,
                field_id=field_id,
                value_id=value_id,
            )
            updated_fields.append(field_name)

        new_version = _optional_int(relay_result.get("newVersion"))
        if new_version is not None:
            item_columns = _table_columns(connection, "items")
            assignments = ["version = ?"]
            values: list[object] = [new_version]
            if "synced" in item_columns:
                assignments.append("synced = 1")
            connection.execute(
                f"update items set {', '.join(assignments)} where itemID = ?",
                (*values, metadata.item_id),
            )

        sync_cache = _patch_parent_sync_cache(
            connection,
            metadata=metadata,
            fields={field: fields[field] for field in updated_fields},
            version=new_version,
        )
        connection.commit()
        after = _read_item_field_values(connection, metadata.item_id)
    finally:
        connection.close()

    return {
        "ok": True,
        "updated": bool(updated_fields),
        "sqlite_path": str(sqlite_path),
        "item_key": metadata.key,
        "item_id": metadata.item_id,
        "zotero_version": new_version,
        "updated_fields": updated_fields,
        "skipped_fields": skipped_fields,
        "before": before,
        "after": after,
        "sync_cache": sync_cache,
    }


def patched_sync_cache_json(
    raw_data: str | None,
    *,
    attachment_key: str,
    version: int,
    storage_hash: str,
    storage_mtime: int,
) -> str | None:
    if not raw_data:
        return None
    try:
        payload = json.loads(raw_data)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    payload["version"] = version
    data = payload.get("data")
    if not isinstance(data, dict):
        data = {}
        payload["data"] = data
    data["key"] = data.get("key") or attachment_key
    data["version"] = version
    data["md5"] = storage_hash
    data["mtime"] = storage_mtime
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _field_id(connection: sqlite3.Connection, field_name: str) -> int | None:
    row = connection.execute(
        "select fieldID from fields where fieldName = ? limit 1",
        (field_name,),
    ).fetchone()
    return int(row["fieldID"]) if row is not None else None


def _item_data_value_id(connection: sqlite3.Connection, value: str) -> int:
    row = connection.execute(
        "select valueID from itemDataValues where value = ? limit 1",
        (value,),
    ).fetchone()
    if row is not None:
        return int(row["valueID"])
    cursor = connection.execute(
        "insert into itemDataValues (value) values (?)",
        (value,),
    )
    return _required_lastrowid(cursor)


def _upsert_item_data(
    connection: sqlite3.Connection,
    *,
    item_id: int,
    field_id: int,
    value_id: int,
) -> None:
    row = connection.execute(
        "select 1 from itemData where itemID = ? and fieldID = ? limit 1",
        (item_id, field_id),
    ).fetchone()
    if row is None:
        connection.execute(
            "insert into itemData (itemID, fieldID, valueID) values (?, ?, ?)",
            (item_id, field_id, value_id),
        )
        return
    connection.execute(
        "update itemData set valueID = ? where itemID = ? and fieldID = ?",
        (value_id, item_id, field_id),
    )


def _insert_parent_item(
    connection: sqlite3.Connection,
    *,
    source_item_id: int | None,
    parent_key: str,
    parent_type: str,
    parent_title: str,
    parent_version: int | None,
) -> int:
    item_columns = _table_columns(connection, "items")
    type_row = connection.execute(
        "select itemTypeID from itemTypes where typeName = ? limit 1",
        (parent_type,),
    ).fetchone()
    if type_row is None and parent_type != "document":
        type_row = connection.execute(
            "select itemTypeID from itemTypes where typeName = 'document' limit 1",
        ).fetchone()
    if type_row is None:
        type_row = connection.execute(
            "select itemTypeID from itemTypes where typeName not in ('attachment', 'note', 'annotation') limit 1",
        ).fetchone()
    if type_row is None:
        raise RuntimeError("No suitable Zotero parent item type is available in local SQLite.")

    pdf_row = connection.execute(
        "select * from items where itemID = ? limit 1",
        (source_item_id,),
    ).fetchone() if source_item_id is not None else None
    values: dict[str, object] = {
        "itemTypeID": int(type_row["itemTypeID"]),
        "key": parent_key,
    }
    now = datetime.now(UTC)
    sqlite_timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
    for column in ("dateAdded", "dateModified", "clientDateModified"):
        if column in item_columns:
            values[column] = sqlite_timestamp
    if "libraryID" in item_columns and pdf_row is not None and "libraryID" in pdf_row.keys():
        values["libraryID"] = pdf_row["libraryID"]
    elif "libraryID" in item_columns:
        library_row = connection.execute(
            "select libraryID from items where libraryID is not null limit 1",
        ).fetchone()
        if library_row is not None:
            values["libraryID"] = library_row["libraryID"]
    if "version" in item_columns and parent_version is not None:
        values["version"] = parent_version
    if "synced" in item_columns:
        values["synced"] = 1

    columns = [column for column in values if column in item_columns]
    placeholders = ", ".join("?" for _ in columns)
    cursor = connection.execute(
        f"insert into items ({', '.join(columns)}) values ({placeholders})",
        tuple(values[column] for column in columns),
    )
    return _required_lastrowid(cursor)


def _insert_attachment_item(
    connection: sqlite3.Connection,
    *,
    attachment: LocalAttachment,
    parent_item_id: int,
    version: int | None,
) -> int:
    item_columns = _table_columns(connection, "items")
    attachment_type = connection.execute(
        "select itemTypeID from itemTypes where typeName = 'attachment' limit 1",
    ).fetchone()
    if attachment_type is None:
        raise RuntimeError("No Zotero attachment item type is available in local SQLite.")
    parent_row = connection.execute(
        "select * from items where itemID = ? limit 1",
        (parent_item_id,),
    ).fetchone()
    now = datetime.now(UTC)
    sqlite_timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
    item_values: dict[str, object] = {
        "itemTypeID": int(attachment_type["itemTypeID"]),
        "key": attachment.key,
    }
    for column in ("dateAdded", "dateModified", "clientDateModified"):
        if column in item_columns:
            item_values[column] = sqlite_timestamp
    if "libraryID" in item_columns and parent_row is not None and "libraryID" in parent_row.keys():
        item_values["libraryID"] = parent_row["libraryID"]
    if "version" in item_columns and version is not None:
        item_values["version"] = version
    if "synced" in item_columns:
        item_values["synced"] = 1

    item_insert_columns = [column for column in item_values if column in item_columns]
    cursor = connection.execute(
        (
            f"insert into items ({', '.join(item_insert_columns)}) "
            f"values ({', '.join('?' for _ in item_insert_columns)})"
        ),
        tuple(item_values[column] for column in item_insert_columns),
    )
    item_id = _required_lastrowid(cursor)

    attachment_columns = _table_columns(connection, "itemAttachments")
    attachment_values: dict[str, object] = {
        "itemID": item_id,
        "parentItemID": parent_item_id,
        "linkMode": attachment.link_mode if attachment.link_mode is not None else 0,
        "contentType": attachment.content_type or "application/pdf",
        "path": attachment.zotero_path or f"storage:{attachment.filename}",
        "syncState": 2,
    }
    insert_columns = [column for column in attachment_values if column in attachment_columns]
    connection.execute(
        (
            f"insert into itemAttachments ({', '.join(insert_columns)}) "
            f"values ({', '.join('?' for _ in insert_columns)})"
        ),
        tuple(attachment_values[column] for column in insert_columns),
    )
    return item_id


def _sync_parent_collections(
    connection: sqlite3.Connection,
    *,
    parent_item_id: int,
    attachment_item_id: int,
    collection_keys: list[str],
) -> dict[str, Any]:
    if (
        not collection_keys
        or not _table_exists(connection, "collections")
        or not _table_exists(connection, "collectionItems")
    ):
        return {"updated": False, "reason": "no_collections"}
    rows = connection.execute(
        f"select collectionID from collections where key in ({', '.join('?' for _ in collection_keys)})",
        tuple(collection_keys),
    ).fetchall()
    collection_ids = [int(row["collectionID"]) for row in rows]
    added = 0
    removed = 0
    for collection_id in collection_ids:
        existing = connection.execute(
            "select 1 from collectionItems where collectionID = ? and itemID = ? limit 1",
            (collection_id, parent_item_id),
        ).fetchone()
        if existing is None:
            connection.execute(
                "insert into collectionItems (collectionID, itemID) values (?, ?)",
                (collection_id, parent_item_id),
            )
            added += 1
        cursor = connection.execute(
            "delete from collectionItems where collectionID = ? and itemID = ?",
            (collection_id, attachment_item_id),
        )
        removed += cursor.rowcount if cursor.rowcount is not None else 0
    return {
        "updated": bool(added or removed),
        "addedToParent": added,
        "removedFromAttachment": removed,
        "collectionIds": collection_ids,
    }


def _read_item_field_values(connection: sqlite3.Connection, item_id: int) -> dict[str, str]:
    rows = connection.execute(
        """
        select f.fieldName, v.value
        from itemData d
        join fields f on f.fieldID = d.fieldID
        join itemDataValues v on v.valueID = d.valueID
        where d.itemID = ?
        order by f.fieldName collate nocase asc
        """,
        (item_id,),
    ).fetchall()
    return {str(row["fieldName"]): str(row["value"] or "") for row in rows}


def _patch_parent_sync_cache(
    connection: sqlite3.Connection,
    *,
    metadata: LocalItemMetadata,
    fields: dict[str, str],
    version: int | None,
) -> dict[str, Any]:
    if not _table_exists(connection, "syncCache"):
        return {"updated": False, "reason": "sync_cache_missing"}
    rows = connection.execute(
        """
        select rowid, version, data
        from syncCache
        where key = ?
        """,
        (metadata.key,),
    ).fetchall()
    updated = 0
    for row in rows:
        raw = row["data"]
        if not raw:
            continue
        try:
            payload = json.loads(str(raw))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        data = payload.get("data")
        target = data if isinstance(data, dict) else payload
        target.update(fields)
        if version is not None:
            target["version"] = version
            payload["version"] = version
        connection.execute(
            "update syncCache set version = coalesce(?, version), data = ? where rowid = ?",
            (
                version,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                row["rowid"],
            ),
        )
        updated += 1
    return {"updated": bool(updated), "rows": updated}


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "select 1 from sqlite_master where type = 'table' and name = ? limit 1",
        (table_name,),
    ).fetchone()
    return row is not None


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    if not _table_exists(connection, table_name):
        return set()
    return {str(row["name"]) for row in connection.execute(f"pragma table_info({table_name})")}


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _required_lastrowid(cursor: sqlite3.Cursor) -> int:
    value = cursor.lastrowid
    if value is None:
        raise RuntimeError("SQLite insert did not produce a row id.")
    return value
