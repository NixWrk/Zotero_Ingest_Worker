from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from zotero_ingest_worker.full_text_inventory import FullTextAttachmentRecord
from zotero_ingest_worker.source_html_maintenance import (
    cleanup_source_html_library,
    cleanup_source_html_records,
)


def test_cleanup_source_html_records_trashes_missing_and_duplicate_records(tmp_path: Path) -> None:
    keep = tmp_path / "storage" / "HTMLKEEP" / "Article [SOURCE HTML].html"
    old = tmp_path / "storage" / "HTMLOLD" / "Article [SOURCE HTML].html"
    keep.parent.mkdir(parents=True)
    old.parent.mkdir(parents=True)
    keep.write_text("newer larger article", encoding="utf-8")
    old.write_text("old", encoding="utf-8")
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    trash_calls: list[dict[str, Any]] = []

    result = cleanup_source_html_records(
        metadata=metadata,
        records=[
            FullTextAttachmentRecord(
                key="HTMLKEEP",
                content_type="text/html",
                path="storage:Article [SOURCE HTML].html",
                title="Article [source HTML]",
                file_path=str(keep),
                exists=True,
            ),
            FullTextAttachmentRecord(
                key="HTMLOLD",
                content_type="text/html",
                path="storage:Article [SOURCE HTML].html",
                title="Article [source HTML]",
                file_path=str(old),
                exists=True,
            ),
            FullTextAttachmentRecord(
                key="HTMLMISS",
                content_type="text/html",
                path="storage:Article [SOURCE HTML].html",
                title="Article [source HTML]",
                file_path=str(tmp_path / "storage" / "HTMLMISS" / "Article [SOURCE HTML].html"),
                exists=False,
            ),
        ],
        storage_dir=tmp_path / "storage",
        trash_attachment=lambda **kwargs: trash_calls.append(kwargs) or {"ok": True, "dryRun": kwargs["dry_run"]},
        dry_run=True,
    )

    assert result["ok"] is True
    assert result["keep_key"] == "HTMLKEEP"
    assert result["candidate_count"] == 2
    assert [(item["key"], item["reason"]) for item in result["candidates"]] == [
        ("HTMLOLD", "duplicate_source_html"),
        ("HTMLMISS", "missing_file"),
    ]
    assert trash_calls == []
    assert [item["relay"] for item in result["trashed"]] == [
        {"ok": True, "dryRun": True, "wouldTrash": True},
        {"ok": True, "dryRun": True, "wouldTrash": True},
    ]


def test_cleanup_source_html_records_fails_closed_when_trash_fails(tmp_path: Path) -> None:
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )

    result = cleanup_source_html_records(
        metadata=metadata,
        records=[
            FullTextAttachmentRecord(
                key="HTMLMISS",
                content_type="text/html",
                path="storage:Article [SOURCE HTML].html",
                title="Article [source HTML]",
                file_path=str(tmp_path / "storage" / "HTMLMISS" / "Article [SOURCE HTML].html"),
                exists=False,
            ),
        ],
        storage_dir=tmp_path / "storage",
        trash_attachment=lambda **_kwargs: {"ok": False, "error": "relay down"},
        dry_run=False,
    )

    assert result["ok"] is False
    assert result["errors"] == [{"key": "HTMLMISS", "error": "relay down"}]


def test_cleanup_source_html_library_reports_nested_failure(tmp_path: Path) -> None:
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    store = SimpleNamespace(
        library_id="LIB1",
        config=SimpleNamespace(zotero_data_dir=tmp_path, resolved_storage_dir=tmp_path / "storage"),
        iter_regular_items=lambda **_kwargs: [metadata],
        full_text_inventory=lambda _metadata: SimpleNamespace(
            attachments=(
                FullTextAttachmentRecord(
                    key="HTMLMISS",
                    content_type="text/html",
                    path="storage:Article [SOURCE HTML].html",
                    title="Article [source HTML]",
                    file_path=str(tmp_path / "storage" / "HTMLMISS" / "Article [SOURCE HTML].html"),
                    exists=False,
                ),
            )
        ),
    )

    result = cleanup_source_html_library(
        store=store,
        trash_attachment=lambda **_kwargs: {"ok": False, "error": "relay down"},
        max_items=10,
        dry_run=False,
    )

    assert result["ok"] is False
    assert result["affected_parents"] == 1
    assert result["candidate_count"] == 1
