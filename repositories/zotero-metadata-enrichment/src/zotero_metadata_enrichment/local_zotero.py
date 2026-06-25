from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Iterable

from .attachment_types import base_content_type, is_html_attachment, is_pdf_attachment
from .models import LocalAttachment, LocalItemMetadata


class LocalZoteroReader:
    def __init__(self, data_dir: str | Path, *, storage_dir: str | Path | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.storage_dir = Path(storage_dir) if storage_dir is not None else self.data_dir / "storage"
        self.sqlite_path = self.data_dir / "zotero.sqlite"
        self.library_id = library_id_for_data_dir(self.data_dir)

    def iter_pdf_attachments(self, *, max_items: int | None = None) -> Iterable[LocalAttachment]:
        connection = connect_readonly(self.sqlite_path)
        connection.row_factory = sqlite3.Row
        try:
            limit_clause = "" if max_items is None else "limit ?"
            params: tuple[int, ...] = () if max_items is None else (max_items,)
            rows = connection.execute(
                f"""
                select
                  i.itemID,
                  i.key,
                  i.dateModified,
                  ia.parentItemID,
                  parent.key as parentKey,
                  ia.linkMode,
                  ia.contentType,
                  ia.path
                from itemAttachments ia
                join items i on i.itemID = ia.itemID
                left join items parent on parent.itemID = ia.parentItemID
                left join deletedItems di on di.itemID = i.itemID
                where di.itemID is null
                  and (
                    lower(coalesce(ia.contentType, '')) = 'application/pdf'
                    or lower(coalesce(ia.path, '')) like '%.pdf%'
                  )
                order by i.dateModified desc
                {limit_clause}
                """,
                params,
            ).fetchall()
        finally:
            connection.close()
        for row in rows:
            attachment = self._attachment_from_row(row)
            if attachment is not None and is_pdf_attachment(
                content_type=attachment.content_type,
                path=attachment.zotero_path,
                file_path=str(attachment.file_path),
            ):
                yield attachment

    def iter_regular_items(
        self,
        *,
        max_items: int | None = None,
        collection: str | None = None,
    ) -> Iterable[LocalItemMetadata]:
        connection = connect_readonly(self.sqlite_path)
        connection.row_factory = sqlite3.Row
        try:
            limit_clause = "" if max_items is None else "limit ?"
            if collection:
                params: tuple[object, ...] = (
                    (collection, collection)
                    if max_items is None
                    else (collection, collection, max_items)
                )
                rows = connection.execute(
                    f"""
                    select distinct i.itemID, i.key, i.version, i.dateModified, it.typeName
                    from collections c
                    join collectionItems ci on ci.collectionID = c.collectionID
                    join items i on i.itemID = ci.itemID
                    left join itemTypes it on it.itemTypeID = i.itemTypeID
                    left join deletedItems di on di.itemID = i.itemID
                    where (c.collectionName = ? or c.key = ?)
                      and di.itemID is null
                      and coalesce(it.typeName, '') not in ('attachment', 'note', 'annotation')
                    order by i.dateModified desc
                    {limit_clause}
                    """,
                    params,
                ).fetchall()
            else:
                params = () if max_items is None else (max_items,)
                rows = connection.execute(
                    f"""
                    select i.itemID, i.key, i.version, i.dateModified, it.typeName
                    from items i
                    left join itemTypes it on it.itemTypeID = i.itemTypeID
                    left join deletedItems di on di.itemID = i.itemID
                    where di.itemID is null
                      and coalesce(it.typeName, '') not in ('attachment', 'note', 'annotation')
                    order by i.dateModified desc
                    {limit_clause}
                    """,
                    params,
                ).fetchall()
            for row in rows:
                item_id = int(row["itemID"])
                yield LocalItemMetadata(
                    library_id=self.library_id,
                    data_dir=self.data_dir,
                    key=str(row["key"]),
                    item_id=item_id,
                    version=optional_int(row["version"]),
                    item_type=row["typeName"],
                    date_modified=row["dateModified"],
                    fields=item_fields(connection, item_id),
                    creators=item_creators(connection, item_id),
                    tags=item_tags(connection, item_id),
                    collections=item_collections(connection, item_id),
                    relations=item_relations(connection, item_id),
                )
        finally:
            connection.close()

    def item_full_text_inventory(self, item: LocalItemMetadata) -> dict[str, object]:
        connection = connect_readonly(self.sqlite_path)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                select i.key, ia.contentType, ia.path
                from itemAttachments ia
                join items i on i.itemID = ia.itemID
                left join deletedItems di on di.itemID = i.itemID
                where ia.parentItemID = ?
                  and di.itemID is null
                """,
                (item.item_id,),
            ).fetchall()
        finally:
            connection.close()
        pdf_count = 0
        html_count = 0
        attachments: list[dict[str, str]] = []
        for row in rows:
            content_type = base_content_type(row["contentType"])
            path = str(row["path"] or "")
            is_pdf = is_pdf_attachment(content_type=content_type, path=path)
            is_html = is_html_attachment(content_type=content_type, path=path)
            if is_pdf:
                pdf_count += 1
            if is_html:
                html_count += 1
            attachments.append({"key": str(row["key"]), "content_type": content_type, "path": path})
        return {
            "pdf_count": pdf_count,
            "html_count": html_count,
            "has_pdf": pdf_count > 0,
            "has_html": html_count > 0,
            "attachments": attachments,
        }

    def get_attachment(self, key: str) -> LocalAttachment:
        key = key.strip()
        connection = connect_readonly(self.sqlite_path)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                """
                select
                  i.itemID,
                  i.key,
                  i.dateModified,
                  ia.parentItemID,
                  parent.key as parentKey,
                  ia.linkMode,
                  ia.contentType,
                  ia.path
                from itemAttachments ia
                join items i on i.itemID = ia.itemID
                left join items parent on parent.itemID = ia.parentItemID
                left join deletedItems di on di.itemID = i.itemID
                where i.key = ?
                  and di.itemID is null
                limit 1
                """,
                (key,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise FileNotFoundError(f"Attachment was not found: {key}")
        attachment = self._attachment_from_row(row)
        if attachment is None:
            raise FileNotFoundError(f"Attachment file was not found: {key}")
        return attachment

    def get_parent_metadata_for_attachment(
        self,
        attachment: LocalAttachment,
        *,
        allow_standalone: bool = False,
    ) -> LocalItemMetadata | None:
        parent_key = attachment.parent_key
        if parent_key:
            return self.get_item_metadata(parent_key)
        if allow_standalone:
            return self.get_item_metadata(attachment.key)
        return None

    def get_item_metadata(self, key: str) -> LocalItemMetadata:
        key = key.strip()
        connection = connect_readonly(self.sqlite_path)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                """
                select i.itemID, i.key, i.version, i.dateModified, it.typeName
                from items i
                left join itemTypes it on it.itemTypeID = i.itemTypeID
                left join deletedItems di on di.itemID = i.itemID
                where i.key = ?
                  and di.itemID is null
                limit 1
                """,
                (key,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(f"Item was not found: {key}")
            item_id = int(row["itemID"])
            return LocalItemMetadata(
                library_id=self.library_id,
                data_dir=self.data_dir,
                key=str(row["key"]),
                item_id=item_id,
                version=optional_int(row["version"]),
                item_type=row["typeName"],
                date_modified=row["dateModified"],
                fields=item_fields(connection, item_id),
                creators=item_creators(connection, item_id),
                tags=item_tags(connection, item_id),
                collections=item_collections(connection, item_id),
                relations=item_relations(connection, item_id),
            )
        finally:
            connection.close()

    def _attachment_from_row(self, row: sqlite3.Row) -> LocalAttachment | None:
        key = str(row["key"])
        zotero_path = row["path"]
        file_path = resolve_attachment_path(
            storage_dir=self.storage_dir,
            key=key,
            zotero_path=zotero_path,
        )
        if file_path is None or not file_path.exists():
            return None
        return LocalAttachment(
            library_id=self.library_id,
            data_dir=self.data_dir,
            storage_dir=self.storage_dir,
            key=key,
            item_id=optional_int(row["itemID"]),
            parent_item_id=optional_int(row["parentItemID"]),
            parent_key=row["parentKey"],
            date_modified=row["dateModified"],
            link_mode=optional_int(row["linkMode"]),
            content_type=row["contentType"],
            zotero_path=zotero_path,
            file_path=file_path,
        )


def library_id_for_data_dir(data_dir: Path) -> str:
    resolved = str(data_dir.resolve()).lower()
    digest = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:8]
    label = re.sub(r"[^A-Za-z0-9]+", "_", data_dir.name).strip("_") or "zotero"
    return f"{label}_{digest}"


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.as_posix()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.execute("pragma query_only = true")
    return connection


def resolve_attachment_path(*, storage_dir: Path, key: str, zotero_path: str | None) -> Path | None:
    attachment_dir = storage_dir / key
    if zotero_path and zotero_path.startswith("storage:"):
        return attachment_dir / zotero_path.removeprefix("storage:")
    if zotero_path:
        candidate = Path(zotero_path)
        if candidate.exists():
            return candidate
    if attachment_dir.exists():
        pdfs = sorted(attachment_dir.glob("*.pdf"), key=lambda path: path.stat().st_mtime, reverse=True)
        if pdfs:
            return pdfs[0]
    return None


def item_fields(connection: sqlite3.Connection, item_id: int) -> dict[str, str]:
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
    return {str(row["fieldName"]): str(row["value"] or "") for row in rows if row["fieldName"]}


def item_creators(connection: sqlite3.Connection, item_id: int) -> list[dict[str, object]]:
    try:
        rows = connection.execute(
            """
            select c.firstName, c.lastName, c.fieldMode, ct.creatorType, ic.orderIndex
            from itemCreators ic
            join creators c on c.creatorID = ic.creatorID
            left join creatorTypes ct on ct.creatorTypeID = ic.creatorTypeID
            where ic.itemID = ?
            order by ic.orderIndex asc
            """,
            (item_id,),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [
        {
            "firstName": row["firstName"],
            "lastName": row["lastName"],
            "fieldMode": optional_int(row["fieldMode"]),
            "creatorType": row["creatorType"],
            "orderIndex": optional_int(row["orderIndex"]),
        }
        for row in rows
    ]


def item_tags(connection: sqlite3.Connection, item_id: int) -> list[str]:
    try:
        rows = connection.execute(
            """
            select t.name
            from itemTags it
            join tags t on t.tagID = it.tagID
            where it.itemID = ?
            order by t.name collate nocase asc
            """,
            (item_id,),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [str(row["name"]) for row in rows if row["name"]]


def item_collections(connection: sqlite3.Connection, item_id: int) -> list[dict[str, object]]:
    try:
        rows = connection.execute(
            """
            select c.collectionID, c.key, c.collectionName
            from collectionItems ci
            join collections c on c.collectionID = ci.collectionID
            where ci.itemID = ?
            order by c.collectionName collate nocase asc
            """,
            (item_id,),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [
        {
            "collectionID": optional_int(row["collectionID"]),
            "key": row["key"],
            "name": row["collectionName"],
        }
        for row in rows
    ]


def item_relations(connection: sqlite3.Connection, item_id: int) -> list[dict[str, object]]:
    try:
        rows = connection.execute(
            """
            select predicate, object
            from relations
            where subject = ?
            order by predicate collate nocase asc, object collate nocase asc
            """,
            (item_id,),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [{"predicate": row["predicate"], "object": row["object"]} for row in rows]


def optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
