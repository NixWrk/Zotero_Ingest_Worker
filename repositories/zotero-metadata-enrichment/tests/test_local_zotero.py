from __future__ import annotations

import sqlite3
from pathlib import Path

from zotero_metadata_enrichment.local_zotero import LocalZoteroReader


def test_local_zotero_reader_reads_extended_parent_metadata(tmp_path: Path) -> None:
    data_dir = tmp_path / "Zotero_Test_Data"
    storage_dir = data_dir / "storage" / "PDF1234"
    storage_dir.mkdir(parents=True)
    (storage_dir / "paper.pdf").write_bytes(b"%PDF")
    newer_storage_dir = data_dir / "storage" / "PDFNEW1"
    newer_storage_dir.mkdir(parents=True)
    (newer_storage_dir / "newer.pdf").write_bytes(b"%PDF")
    html_storage_dir = data_dir / "storage" / "HTMLPDF1"
    html_storage_dir.mkdir(parents=True)
    (html_storage_dir / "paper.pdf.html").write_text("<html><body>article</body></html>", encoding="utf-8")
    sqlite_path = data_dir / "zotero.sqlite"

    connection = sqlite3.connect(sqlite_path)
    try:
        connection.executescript(
            """
            create table itemTypes (itemTypeID integer primary key, typeName text);
            create table items (
                itemID integer primary key,
                itemTypeID integer,
                dateModified text,
                key text,
                version integer
            );
            create table deletedItems (itemID integer primary key);
            create table itemAttachments (
                itemID integer primary key,
                parentItemID integer,
                linkMode integer,
                contentType text,
                path text
            );
            create table fields (fieldID integer primary key, fieldName text);
            create table itemDataValues (valueID integer primary key, value text);
            create table itemData (itemID integer, fieldID integer, valueID integer);
            create table creatorTypes (creatorTypeID integer primary key, creatorType text);
            create table creators (creatorID integer primary key, firstName text, lastName text, fieldMode integer);
            create table itemCreators (itemID integer, creatorID integer, creatorTypeID integer, orderIndex integer);
            create table tags (tagID integer primary key, name text);
            create table itemTags (itemID integer, tagID integer);
            create table collections (collectionID integer primary key, key text, collectionName text);
            create table collectionItems (collectionID integer, itemID integer);
            create table relations (subject integer, predicate text, object text);
            insert into itemTypes values (1, 'journalArticle'), (2, 'attachment');
            insert into items values (10, 1, '2026-01-01', 'PARENT1', 42);
            insert into items values (20, 2, '2026-01-02', 'PDF1234', 7);
            insert into items values (21, 2, '2026-01-03', 'PDFNEW1', 7);
            insert into items values (22, 2, '2026-01-02', 'HTMLPDF1', 7);
            insert into itemAttachments values (20, 10, 0, 'application/pdf', 'storage:paper.pdf');
            insert into itemAttachments values (21, null, 0, 'application/pdf', 'storage:newer.pdf');
            insert into itemAttachments values (22, 10, 0, 'text/html', 'storage:paper.pdf.html');
            insert into fields values (1, 'title'), (2, 'DOI'), (3, 'extra');
            insert into itemDataValues values
              (1, 'A Careful Metadata Pipeline'),
              (2, '10.48550/arXiv.2401.01234'),
              (3, 'arXiv:2401.01234 [cs.DL]');
            insert into itemData values (10, 1, 1), (10, 2, 2), (10, 3, 3);
            insert into creatorTypes values (1, 'author');
            insert into creators values (1, 'Ada', 'Lovelace', 0);
            insert into itemCreators values (10, 1, 1, 0);
            insert into tags values (1, 'Digital Libraries');
            insert into itemTags values (10, 1);
            insert into collections values (1, 'COLL1', 'Meine');
            insert into collectionItems values (1, 10);
            insert into relations values (10, 'dc:relation', 'https://arxiv.org/abs/2401.01234');
            """
        )
        connection.commit()
    finally:
        connection.close()

    reader = LocalZoteroReader(data_dir)
    attachment = reader.get_attachment("PDF1234")
    metadata = reader.get_parent_metadata_for_attachment(attachment)
    items = list(reader.iter_regular_items())
    pdf_attachments = list(reader.iter_pdf_attachments())

    assert metadata is not None
    assert metadata.key == "PARENT1"
    assert metadata.version == 42
    assert metadata.title == "A Careful Metadata Pipeline"
    assert metadata.creators[0]["lastName"] == "Lovelace"
    assert metadata.tags == ["Digital Libraries"]
    assert metadata.collections[0]["name"] == "Meine"
    assert metadata.relations[0]["object"] == "https://arxiv.org/abs/2401.01234"
    assert [item.key for item in items] == ["PARENT1"]
    assert [item.key for item in pdf_attachments] == ["PDFNEW1", "PDF1234"]
    assert [item.key for item in reader.iter_pdf_attachments(max_items=1)] == ["PDFNEW1"]
    inventory = reader.item_full_text_inventory(metadata)
    assert inventory["has_pdf"] is True
    assert inventory["has_html"] is True
    assert inventory["pdf_count"] == 1
    assert inventory["html_count"] == 1

    connection = sqlite3.connect(sqlite_path)
    try:
        connection.executescript(
            """
            insert into items values (23, 2, '2026-01-04', 'HTMLMHT1', 8);
            insert into itemAttachments values (23, 10, 0, 'multipart/related', 'storage:article [SOURCE HTML].mhtml');
            """
        )
        connection.commit()
    finally:
        connection.close()

    inventory = reader.item_full_text_inventory(metadata)
    assert inventory["has_html"] is True
    assert inventory["html_count"] == 2
