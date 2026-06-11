from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Iterable

from .config import WorkerConfig
from .full_text_inventory import (
    FullTextAttachmentRecord,
    FullTextInventory,
    resolved_attachment_path,
)
from .local_zotero_paths import (
    library_id_for_data_dir,
    looks_like_generated_html,
    resolve_attachment_path_for_suffixes,
    safe_exists,
    safe_mtime,
)
from .local_zotero_sqlite import connect_readonly_sqlite


@dataclass(frozen=True)
class LocalAttachment:
    library_id: str
    data_dir: Path
    storage_dir: Path
    key: str
    item_id: int | None
    parent_item_id: int | None
    date_modified: str | None
    link_mode: int | None
    content_type: str | None
    zotero_path: str | None
    file_path: Path
    parent_key: str | None = None

    @property
    def filename(self) -> str:
        return self.file_path.name

    @property
    def state_key(self) -> str:
        return f"{self.library_id}_{self.key}"

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["data_dir"] = str(self.data_dir)
        payload["storage_dir"] = str(self.storage_dir)
        payload["file_path"] = str(self.file_path)
        return payload


@dataclass(frozen=True)
class LocalItemMetadata:
    library_id: str
    data_dir: Path
    key: str
    item_id: int
    version: int | None
    item_type: str | None
    date_modified: str | None
    fields: dict[str, str]
    creators: list[dict[str, object]]
    tags: list[str]
    collections: list[dict[str, object]]
    relations: list[dict[str, object]]

    @property
    def title(self) -> str:
        return self.fields.get("title", "")

    def to_dict(self) -> dict[str, object]:
        return {
            "library_id": self.library_id,
            "data_dir": str(self.data_dir),
            "key": self.key,
            "item_id": self.item_id,
            "version": self.version,
            "item_type": self.item_type,
            "date_modified": self.date_modified,
            "fields": self.fields,
            "creators": self.creators,
            "tags": self.tags,
            "collections": self.collections,
            "relations": self.relations,
        }


class LocalZoteroStore:
    def __init__(self, config: WorkerConfig):
        self.config = config
        self.library_id = library_id_for_data_dir(config.zotero_data_dir)

    def count_sqlite_pdf_attachments(self) -> int:
        connection = connect_readonly_sqlite(self.config.zotero_sqlite_path)
        try:
            row = connection.execute(
                """
                select count(*)
                from itemAttachments ia
                join items i on i.itemID = ia.itemID
                left join deletedItems di on di.itemID = i.itemID
                where di.itemID is null
                  and (
                    lower(coalesce(ia.contentType, '')) = 'application/pdf'
                    or lower(coalesce(ia.path, '')) like '%.pdf%'
                  )
                """
            ).fetchone()
            return int(row[0])
        finally:
            connection.close()

    def iter_pdf_attachments(self, *, max_items: int) -> Iterable[LocalAttachment]:
        seen: set[str] = set()
        known_sqlite_keys = self._sqlite_attachment_keys()
        for attachment in self._iter_sqlite_pdf_attachments(max_items=max_items):
            seen.add(attachment.key)
            yield attachment
        if len(seen) >= max_items:
            return
        for attachment in self._iter_storage_pdf_fallback(max_items=max_items - len(seen)):
            if attachment.key not in seen and attachment.key not in known_sqlite_keys:
                yield attachment

    def iter_storage_pdf_paths(self) -> Iterable[Path]:
        storage_root = self.config.resolved_storage_dir
        if not storage_root.exists():
            return
        for folder in sorted(storage_root.iterdir(), key=lambda path: path.name.lower()):
            try:
                is_dir = folder.is_dir()
            except OSError:
                continue
            if not is_dir:
                continue
            try:
                children = sorted(folder.iterdir(), key=lambda path: path.name.lower())
            except OSError:
                continue
            for file_path in children:
                if file_path.is_file() and file_path.suffix.lower() == ".pdf":
                    yield file_path

    def iter_html_attachments(
        self,
        *,
        max_items: int,
        generated_only: bool = False,
    ) -> Iterable[LocalAttachment]:
        connection = connect_readonly_sqlite(self.config.zotero_sqlite_path)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                select
                  i.itemID,
                  i.key,
                  i.dateModified,
                  ia.parentItemID,
                  parent.key AS parentKey,
                  ia.linkMode,
                  ia.contentType,
                  ia.path
                from itemAttachments ia
                join items i on i.itemID = ia.itemID
                left join items parent on parent.itemID = ia.parentItemID
                left join deletedItems di on di.itemID = i.itemID
                where di.itemID is null
                  and (
                    lower(coalesce(ia.contentType, '')) in ('text/html', 'application/xhtml+xml')
                    or lower(coalesce(ia.path, '')) like '%.html%'
                    or lower(coalesce(ia.path, '')) like '%.htm%'
                  )
                order by i.dateModified desc
                limit ?
                """,
                (max_items,),
            ).fetchall()
        finally:
            connection.close()

        for row in rows:
            attachment = self._html_attachment_from_row(row)
            if attachment is None:
                continue
            if generated_only and not looks_like_generated_html(attachment.filename):
                continue
            yield attachment

    def iter_collection_html_attachments(
        self,
        *,
        collection: str,
        max_items: int,
        generated_only: bool = False,
    ) -> Iterable[LocalAttachment]:
        collection = collection.strip()
        if not collection:
            return

        connection = connect_readonly_sqlite(self.config.zotero_sqlite_path)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                select distinct
                  i.itemID,
                  i.key,
                  i.dateModified,
                  ia.parentItemID,
                  parent.key AS parentKey,
                  ia.linkMode,
                  ia.contentType,
                  ia.path
                from collections c
                join collectionItems ci on ci.collectionID = c.collectionID
                join itemAttachments ia
                  on ia.itemID = ci.itemID
                  or ia.parentItemID = ci.itemID
                join items i on i.itemID = ia.itemID
                left join items parent on parent.itemID = ia.parentItemID
                left join deletedItems di on di.itemID = i.itemID
                where (c.collectionName = ? or c.key = ?)
                  and di.itemID is null
                  and (
                    lower(coalesce(ia.contentType, '')) in ('text/html', 'application/xhtml+xml')
                    or lower(coalesce(ia.path, '')) like '%.html%'
                    or lower(coalesce(ia.path, '')) like '%.htm%'
                  )
                order by i.dateModified desc
                limit ?
                """,
                (collection, collection, max_items),
            ).fetchall()
        finally:
            connection.close()

        for row in rows:
            attachment = self._html_attachment_from_row(row)
            if attachment is None:
                continue
            if generated_only and not looks_like_generated_html(attachment.filename):
                continue
            yield attachment

    def iter_generated_html_files(self, *, max_items: int) -> Iterable[LocalAttachment]:
        """Yield generated HTML files that really exist in local Zotero storage."""
        yielded = 0
        try:
            folders = sorted(
                self.config.resolved_storage_dir.iterdir(),
                key=lambda path: path.name.lower(),
            )
        except OSError:
            return

        for folder in folders:
            if yielded >= max_items:
                break
            try:
                if not folder.is_dir():
                    continue
                children = sorted(folder.iterdir(), key=lambda path: path.name.lower())
            except OSError:
                continue
            for file_path in children:
                if not file_path.is_file() or not looks_like_generated_html(file_path.name):
                    continue
                yield LocalAttachment(
                    library_id=self.library_id,
                    data_dir=self.config.zotero_data_dir,
                    storage_dir=self.config.resolved_storage_dir,
                    key=folder.name,
                    item_id=None,
                    parent_item_id=None,
                    date_modified=None,
                    link_mode=None,
                    content_type="text/html",
                    zotero_path=f"storage:{file_path.name}",
                    file_path=file_path,
                )
                yielded += 1
                break

    def get_attachment(self, key: str) -> LocalAttachment:
        key = key.strip()
        for attachment in self._iter_sqlite_pdf_attachments(max_items=100000):
            if attachment.key == key:
                return attachment
        storage_dir = self.config.resolved_storage_dir / key
        if storage_dir.exists():
            if self._sqlite_attachment_key_exists(key):
                raise FileNotFoundError(f"Local Zotero PDF attachment is deleted or inactive: {key}")
            pdfs = sorted(storage_dir.glob("*.pdf"), key=lambda path: path.stat().st_mtime, reverse=True)
            if pdfs:
                return LocalAttachment(
                    library_id=self.library_id,
                    data_dir=self.config.zotero_data_dir,
                    storage_dir=self.config.resolved_storage_dir,
                    key=key,
                    item_id=None,
                    parent_item_id=None,
                    date_modified=None,
                    link_mode=None,
                    content_type="application/pdf",
                    zotero_path=None,
                    file_path=pdfs[0],
                )
        raise FileNotFoundError(f"Local Zotero PDF attachment was not found: {key}")

    def get_attachment_for_file(self, file_path: Path) -> LocalAttachment:
        resolved_file = file_path.resolve()
        if resolved_file.suffix.lower() != ".pdf":
            raise FileNotFoundError(f"Path is not a PDF file: {file_path}")
        try:
            relative = resolved_file.relative_to(self.config.resolved_storage_dir.resolve())
        except ValueError as exc:
            raise FileNotFoundError(f"PDF is not inside Zotero storage: {file_path}") from exc
        if len(relative.parts) < 2:
            raise FileNotFoundError(f"PDF is not inside an attachment folder: {file_path}")

        key = relative.parts[0]
        try:
            attachment = self.get_attachment(key)
            return replace(attachment, file_path=resolved_file)
        except FileNotFoundError:
            return LocalAttachment(
                library_id=self.library_id,
                data_dir=self.config.zotero_data_dir,
                storage_dir=self.config.resolved_storage_dir,
                key=key,
                item_id=None,
                parent_item_id=None,
                date_modified=None,
                link_mode=None,
                content_type="application/pdf",
                zotero_path=None,
                file_path=resolved_file,
            )

    def iter_collection_pdf_attachments(
        self,
        *,
        collection: str,
        filename_contains: str | None = None,
        max_items: int = 100,
    ) -> Iterable[LocalAttachment]:
        collection = collection.strip()
        if not collection:
            return

        connection = connect_readonly_sqlite(self.config.zotero_sqlite_path)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                select distinct
                  i.itemID,
                  i.key,
                  i.dateModified,
                  ia.parentItemID,
                  parent.key AS parentKey,
                  ia.linkMode,
                  ia.contentType,
                  ia.path
                from collections c
                join collectionItems ci on ci.collectionID = c.collectionID
                join itemAttachments ia
                  on ia.itemID = ci.itemID
                  or ia.parentItemID = ci.itemID
                join items i on i.itemID = ia.itemID
                left join items parent on parent.itemID = ia.parentItemID
                left join deletedItems di on di.itemID = i.itemID
                where (c.collectionName = ? or c.key = ?)
                  and di.itemID is null
                  and (
                    lower(coalesce(ia.contentType, '')) = 'application/pdf'
                    or lower(coalesce(ia.path, '')) like '%.pdf%'
                  )
                order by i.dateModified desc
                limit ?
                """,
                (collection, collection, max_items),
            ).fetchall()
        finally:
            connection.close()

        filename_filter = filename_contains.lower() if filename_contains else None
        for row in rows:
            attachment = self._attachment_from_row(row)
            if attachment is None:
                continue
            if filename_filter and filename_filter not in attachment.filename.lower():
                continue
            yield attachment

    def get_collection_item_keys(self, collection: str) -> set[str]:
        collection = collection.strip()
        if not collection:
            return set()

        connection = connect_readonly_sqlite(self.config.zotero_sqlite_path)
        try:
            rows = connection.execute(
                """
                select distinct i.key
                from collections c
                join collectionItems ci on ci.collectionID = c.collectionID
                join items i on i.itemID = ci.itemID
                left join deletedItems di on di.itemID = i.itemID
                where (c.collectionName = ? or c.key = ?)
                  and di.itemID is null
                """,
                (collection, collection),
            ).fetchall()
        finally:
            connection.close()
        return {str(row[0]) for row in rows if row[0]}

    def iter_regular_items(
        self,
        *,
        max_items: int,
        collection: str | None = None,
    ) -> Iterable[LocalItemMetadata]:
        connection = connect_readonly_sqlite(self.config.zotero_sqlite_path)
        connection.row_factory = sqlite3.Row
        try:
            if collection:
                collection = collection.strip()
                rows = connection.execute(
                    """
                    select distinct
                      i.itemID,
                      i.key,
                      i.version,
                      i.dateModified,
                      it.typeName
                    from collections c
                    join collectionItems ci on ci.collectionID = c.collectionID
                    join items i on i.itemID = ci.itemID
                    left join itemTypes it on it.itemTypeID = i.itemTypeID
                    left join deletedItems di on di.itemID = i.itemID
                    where (c.collectionName = ? or c.key = ?)
                      and di.itemID is null
                      and coalesce(it.typeName, '') not in ('attachment', 'note', 'annotation')
                    order by i.dateModified desc
                    limit ?
                    """,
                    (collection, collection, max_items),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    select
                      i.itemID,
                      i.key,
                      i.version,
                      i.dateModified,
                      it.typeName
                    from items i
                    left join itemTypes it on it.itemTypeID = i.itemTypeID
                    left join deletedItems di on di.itemID = i.itemID
                    where di.itemID is null
                      and coalesce(it.typeName, '') not in ('attachment', 'note', 'annotation')
                    order by i.dateModified desc
                    limit ?
                    """,
                    (max_items,),
                ).fetchall()
            for row in rows:
                item_id = int(row["itemID"])
                yield LocalItemMetadata(
                    library_id=self.library_id,
                    data_dir=self.config.zotero_data_dir,
                    key=str(row["key"]),
                    item_id=item_id,
                    version=_optional_int(row["version"]),
                    item_type=row["typeName"],
                    date_modified=row["dateModified"],
                    fields=_item_fields(connection, item_id),
                    creators=_item_creators(connection, item_id),
                    tags=_item_tags(connection, item_id),
                    collections=_item_collections(connection, item_id),
                    relations=_item_relations(connection, item_id),
                )
        finally:
            connection.close()

    def full_text_inventory(self, item: LocalItemMetadata) -> FullTextInventory:
        connection = connect_readonly_sqlite(self.config.zotero_sqlite_path)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                select
                  ia.itemID,
                  i.key,
                  ia.contentType,
                  ia.path,
                  title_data.value as title
                from itemAttachments ia
                join items i on i.itemID = ia.itemID
                left join (
                  select d.itemID, v.value
                  from itemData d
                  join fields f on f.fieldID = d.fieldID
                  join itemDataValues v on v.valueID = d.valueID
                  where f.fieldName = 'title'
                ) title_data on title_data.itemID = i.itemID
                left join deletedItems di on di.itemID = i.itemID
                where ia.parentItemID = ?
                  and di.itemID is null
                """,
                (item.item_id,),
            ).fetchall()
        finally:
            connection.close()

        attachments: list[FullTextAttachmentRecord] = []
        for row in rows:
            content_type = str(row["contentType"] or "").casefold()
            key = str(row["key"] or "")
            path = str(row["path"] or "")
            file_path = resolved_attachment_path(
                storage_dir=self.config.resolved_storage_dir,
                key=key,
                zotero_path=path,
            )
            attachments.append(
                FullTextAttachmentRecord(
                    key=key,
                    content_type=content_type,
                    path=path,
                    title=str(row["title"] or ""),
                    file_path=str(file_path or ""),
                    exists=safe_exists(file_path) if file_path is not None else None,
                )
            )
        return FullTextInventory(tuple(attachments))

    def item_full_text_inventory(self, item: LocalItemMetadata) -> dict[str, object]:
        return self.full_text_inventory(item).to_dict()

    def get_first_collection_key_for_attachment(self, attachment: LocalAttachment) -> str | None:
        item_ids = [item_id for item_id in (attachment.parent_item_id, attachment.item_id) if item_id]
        if not item_ids:
            return None

        placeholders = ",".join("?" for _ in item_ids)
        connection = connect_readonly_sqlite(self.config.zotero_sqlite_path)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                f"""
                select c.key
                from collections c
                join collectionItems ci on ci.collectionID = c.collectionID
                where ci.itemID in ({placeholders})
                order by c.collectionName collate nocase asc
                limit 1
                """,
                tuple(item_ids),
            ).fetchone()
        finally:
            connection.close()
        return str(row["key"]) if row is not None and row["key"] else None

    def get_parent_key_for_attachment(self, attachment: LocalAttachment) -> str | None:
        if attachment.parent_key:
            return attachment.parent_key
        if not attachment.parent_item_id:
            return None

        connection = connect_readonly_sqlite(self.config.zotero_sqlite_path)
        try:
            row = connection.execute(
                """
                select key
                from items
                where itemID = ?
                limit 1
                """,
                (attachment.parent_item_id,),
            ).fetchone()
        finally:
            connection.close()
        return str(row[0]) if row is not None and row[0] else None

    def get_item_metadata(self, key: str) -> LocalItemMetadata:
        key = key.strip()
        if not key:
            raise FileNotFoundError("Local Zotero item key is empty.")

        connection = connect_readonly_sqlite(self.config.zotero_sqlite_path)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                """
                select
                  i.itemID,
                  i.key,
                  i.version,
                  i.dateModified,
                  it.typeName
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
                raise FileNotFoundError(f"Local Zotero item was not found: {key}")
            item_id = int(row["itemID"])
            return LocalItemMetadata(
                library_id=self.library_id,
                data_dir=self.config.zotero_data_dir,
                key=str(row["key"]),
                item_id=item_id,
                version=_optional_int(row["version"]),
                item_type=row["typeName"],
                date_modified=row["dateModified"],
                fields=_item_fields(connection, item_id),
                creators=_item_creators(connection, item_id),
                tags=_item_tags(connection, item_id),
                collections=_item_collections(connection, item_id),
                relations=_item_relations(connection, item_id),
            )
        finally:
            connection.close()

    def get_parent_metadata_for_attachment(
        self,
        attachment: LocalAttachment,
        *,
        allow_standalone: bool = False,
    ) -> LocalItemMetadata | None:
        parent_key = self.get_parent_key_for_attachment(attachment)
        if parent_key:
            try:
                return self.get_item_metadata(parent_key)
            except FileNotFoundError:
                return None
        if allow_standalone and attachment.item_id is not None:
            try:
                return self.get_item_metadata(attachment.key)
            except FileNotFoundError:
                return None
        return None

    def find_pdf_sibling_by_filename(
        self,
        attachment: LocalAttachment,
        *,
        filename: str,
    ) -> LocalAttachment | None:
        if not attachment.parent_item_id:
            return None
        target = filename.casefold()
        for candidate in self._iter_sqlite_pdf_attachments(max_items=100000):
            if candidate.key == attachment.key:
                continue
            if candidate.parent_item_id != attachment.parent_item_id:
                continue
            if candidate.filename.casefold() == target:
                return candidate
        return None

    def find_pdf_by_filename(
        self,
        *,
        filename: str,
        exclude_key: str | None = None,
    ) -> LocalAttachment | None:
        target = filename.casefold()
        exclude = exclude_key.strip() if exclude_key else None
        for candidate in self._iter_sqlite_pdf_attachments(max_items=100000):
            if exclude and candidate.key == exclude:
                continue
            if candidate.filename.casefold() == target:
                return candidate

        known_sqlite_keys = self._sqlite_attachment_keys()
        for candidate in self._iter_storage_pdf_fallback(max_items=100000):
            if exclude and candidate.key == exclude:
                continue
            if candidate.key in known_sqlite_keys:
                continue
            if candidate.filename.casefold() == target:
                return candidate
        return None

    def _iter_sqlite_pdf_attachments(self, *, max_items: int) -> Iterable[LocalAttachment]:
        connection = connect_readonly_sqlite(self.config.zotero_sqlite_path)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                select
                  i.itemID,
                  i.key,
                  i.dateModified,
                  ia.parentItemID,
                  parent.key AS parentKey,
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
                limit ?
                """,
                (max_items,),
            ).fetchall()
        finally:
            connection.close()

        for row in rows:
            attachment = self._attachment_from_row(row)
            if attachment is not None:
                yield attachment

    def _sqlite_attachment_keys(self) -> set[str]:
        connection = connect_readonly_sqlite(self.config.zotero_sqlite_path)
        try:
            rows = connection.execute(
                """
                select i.key
                from itemAttachments ia
                join items i on i.itemID = ia.itemID
                """
            ).fetchall()
            return {str(row[0]) for row in rows if row[0]}
        finally:
            connection.close()

    def _sqlite_attachment_key_exists(self, key: str) -> bool:
        connection = connect_readonly_sqlite(self.config.zotero_sqlite_path)
        try:
            row = connection.execute(
                """
                select 1
                from itemAttachments ia
                join items i on i.itemID = ia.itemID
                where i.key = ?
                limit 1
                """,
                (key,),
            ).fetchone()
            return row is not None
        finally:
            connection.close()

    def _attachment_from_row(self, row: sqlite3.Row) -> LocalAttachment | None:
        key = str(row["key"])
        zotero_path = row["path"]
        file_path = self._resolve_attachment_path(key=key, zotero_path=zotero_path)
        if file_path is None or not safe_exists(file_path):
            return None
        return LocalAttachment(
            library_id=self.library_id,
            data_dir=self.config.zotero_data_dir,
            storage_dir=self.config.resolved_storage_dir,
            key=key,
            item_id=int(row["itemID"]),
            parent_item_id=row["parentItemID"],
            date_modified=row["dateModified"],
            link_mode=row["linkMode"],
            content_type=row["contentType"],
            zotero_path=zotero_path,
            file_path=file_path,
            parent_key=row["parentKey"],
        )

    def _html_attachment_from_row(self, row: sqlite3.Row) -> LocalAttachment | None:
        key = str(row["key"])
        zotero_path = row["path"]
        storage_dir = self.config.resolved_storage_dir / key
        file_path = self._resolve_attachment_path_for_suffixes(
            key=key,
            zotero_path=zotero_path,
            suffixes=(".html", ".htm"),
            require_exists=False,
        )
        if file_path is None:
            file_path = storage_dir / f"{key}.html"
        return LocalAttachment(
            library_id=self.library_id,
            data_dir=self.config.zotero_data_dir,
            storage_dir=self.config.resolved_storage_dir,
            key=key,
            item_id=int(row["itemID"]),
            parent_item_id=row["parentItemID"],
            date_modified=row["dateModified"],
            link_mode=row["linkMode"],
            content_type=row["contentType"],
            zotero_path=zotero_path,
            file_path=file_path,
            parent_key=row["parentKey"],
        )

    def _resolve_attachment_path(self, *, key: str, zotero_path: str | None) -> Path | None:
        return self._resolve_attachment_path_for_suffixes(
            key=key,
            zotero_path=zotero_path,
            suffixes=(".pdf",),
            require_exists=True,
        )

    def _resolve_attachment_path_for_suffixes(
        self,
        *,
        key: str,
        zotero_path: str | None,
        suffixes: tuple[str, ...],
        require_exists: bool,
    ) -> Path | None:
        return resolve_attachment_path_for_suffixes(
            storage_root=self.config.resolved_storage_dir,
            key=key,
            zotero_path=zotero_path,
            suffixes=suffixes,
            require_exists=require_exists,
        )

    def _iter_storage_pdf_fallback(self, *, max_items: int) -> Iterable[LocalAttachment]:
        storage_root = self.config.resolved_storage_dir
        candidates: list[Path] = []
        try:
            folders = list(storage_root.iterdir())
        except OSError:
            return
        for folder in folders:
            try:
                is_dir = folder.is_dir()
            except OSError:
                continue
            if not is_dir:
                continue
            try:
                candidates.extend(folder.glob("*.pdf"))
            except OSError:
                continue
        candidates.sort(key=safe_mtime, reverse=True)
        for file_path in candidates[:max_items]:
            yield LocalAttachment(
                library_id=self.library_id,
                data_dir=self.config.zotero_data_dir,
                storage_dir=self.config.resolved_storage_dir,
                key=file_path.parent.name,
                item_id=None,
                parent_item_id=None,
                date_modified=None,
                link_mode=None,
                content_type="application/pdf",
                zotero_path=None,
                file_path=file_path,
            )


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_looks_like_generated_html = looks_like_generated_html


def _item_fields(connection: sqlite3.Connection, item_id: int) -> dict[str, str]:
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
    result: dict[str, str] = {}
    for row in rows:
        field = str(row["fieldName"] or "").strip()
        if not field:
            continue
        result[field] = str(row["value"] or "")
    return result


def _item_creators(connection: sqlite3.Connection, item_id: int) -> list[dict[str, object]]:
    try:
        rows = connection.execute(
            """
            select
              c.firstName,
              c.lastName,
              c.fieldMode,
              ct.creatorType,
              ic.orderIndex
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
            "fieldMode": _optional_int(row["fieldMode"]),
            "creatorType": row["creatorType"],
            "orderIndex": _optional_int(row["orderIndex"]),
        }
        for row in rows
    ]


def _item_tags(connection: sqlite3.Connection, item_id: int) -> list[str]:
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


def _item_collections(connection: sqlite3.Connection, item_id: int) -> list[dict[str, object]]:
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
            "collectionID": _optional_int(row["collectionID"]),
            "key": row["key"],
            "name": row["collectionName"],
        }
        for row in rows
    ]


def _item_relations(connection: sqlite3.Connection, item_id: int) -> list[dict[str, object]]:
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
    return [
        {
            "predicate": row["predicate"],
            "object": row["object"],
        }
        for row in rows
    ]
