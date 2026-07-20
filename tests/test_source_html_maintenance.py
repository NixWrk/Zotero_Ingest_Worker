from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import zotero_ingest_worker.source_html_maintenance as source_html_maintenance

from zotero_ingest_worker.full_text_inventory import FullTextAttachmentRecord
from zotero_ingest_worker.source_html_maintenance import (
    cleanup_source_html_library,
    cleanup_source_html_records,
)


def test_cleanup_source_html_records_trashes_missing_and_duplicate_records(
    tmp_path: Path,
) -> None:
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
                file_path=str(
                    tmp_path / "storage" / "HTMLMISS" / "Article [SOURCE HTML].html"
                ),
                exists=False,
            ),
        ],
        storage_dir=tmp_path / "storage",
        trash_attachment=lambda **kwargs: (
            trash_calls.append(kwargs) or {"ok": True, "dryRun": kwargs["dry_run"]}
        ),
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


def test_cleanup_source_html_records_fails_closed_when_trash_fails(
    tmp_path: Path,
) -> None:
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
                file_path=str(
                    tmp_path / "storage" / "HTMLMISS" / "Article [SOURCE HTML].html"
                ),
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
        config=SimpleNamespace(
            zotero_data_dir=tmp_path, resolved_storage_dir=tmp_path / "storage"
        ),
        iter_regular_items=lambda **_kwargs: [metadata],
        full_text_inventory=lambda _metadata: SimpleNamespace(
            attachments=(
                FullTextAttachmentRecord(
                    key="HTMLMISS",
                    content_type="text/html",
                    path="storage:Article [SOURCE HTML].html",
                    title="Article [source HTML]",
                    file_path=str(
                        tmp_path / "storage" / "HTMLMISS" / "Article [SOURCE HTML].html"
                    ),
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


@pytest.mark.parametrize(
    "invalid_ok",
    ["true", 1, None],
    ids=["string-true", "integer-one", "missing"],
)
def test_cleanup_source_html_library_requires_exact_nested_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    invalid_ok: object,
) -> None:
    metadata = SimpleNamespace(key="ITEM1234")
    store = SimpleNamespace(
        library_id="LIB1",
        config=SimpleNamespace(
            zotero_data_dir=tmp_path,
            resolved_storage_dir=tmp_path / "storage",
        ),
        iter_regular_items=lambda **_kwargs: [metadata],
        full_text_inventory=lambda _metadata: SimpleNamespace(attachments=()),
    )
    monkeypatch.setattr(
        source_html_maintenance,
        "cleanup_source_html_records",
        lambda **_kwargs: {
            "ok": invalid_ok,
            "candidate_count": 1,
        },
    )

    result = cleanup_source_html_library(
        store=store,
        trash_attachment=None,
        max_items=10,
        dry_run=True,
    )

    assert result["ok"] is False


def test_cleanup_source_html_records_rejects_non_mapping_trash_result(
    tmp_path: Path,
) -> None:
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
                file_path=str(
                    tmp_path / "storage" / "HTMLMISS" / "Article [SOURCE HTML].html"
                ),
                exists=False,
            ),
        ],
        storage_dir=tmp_path / "storage",
        trash_attachment=lambda **_kwargs: None,  # type: ignore[arg-type,return-value]
        dry_run=False,
    )

    assert result["ok"] is False
    assert result["errors"] == [
        {
            "key": "HTMLMISS",
            "error": "trash_invalid_result: expected mapping, got NoneType",
        }
    ]
    assert result["trashed"][0]["relay"]["reason"] == "trash_invalid_result"


@pytest.mark.parametrize(
    ("raw_exists", "expected"),
    [
        (True, True),
        (False, False),
        (None, None),
        ("false", None),
        (0, None),
        (1, None),
    ],
    ids=["true", "false", "missing", "string-false", "integer-zero", "integer-one"],
)
def test_inventory_exists_requires_boolean_or_missing(
    raw_exists: object,
    expected: bool | None,
) -> None:
    records = source_html_maintenance._source_html_records_from_inventory(
        {
            "attachments": [
                {
                    "key": "HTML1234",
                    "content_type": "text/html",
                    "exists": raw_exists,
                }
            ]
        }
    )

    assert len(records) == 1
    assert records[0].exists is expected


@pytest.mark.parametrize(
    "malformed_item_result",
    [
        None,
        [],
        {"ok": "true", "candidate_count": 1},
        {"ok": True, "candidate_count": "1"},
        {"ok": True, "candidate_count": True},
        {"ok": True, "candidate_count": -1},
    ],
    ids=[
        "none",
        "list",
        "truthy-ok",
        "string-count",
        "boolean-count",
        "negative-count",
    ],
)
def test_cleanup_source_html_library_rejects_malformed_item_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    malformed_item_result: object,
) -> None:
    metadata = SimpleNamespace(key="ITEM1234")
    store = SimpleNamespace(
        library_id="LIB1",
        config=SimpleNamespace(
            zotero_data_dir=tmp_path,
            resolved_storage_dir=tmp_path / "storage",
        ),
        iter_regular_items=lambda **_kwargs: [metadata],
        full_text_inventory=lambda _metadata: SimpleNamespace(attachments=()),
    )
    monkeypatch.setattr(
        source_html_maintenance,
        "cleanup_source_html_records",
        lambda **_kwargs: malformed_item_result,
    )

    result = cleanup_source_html_library(
        store=store,
        trash_attachment=None,
        max_items=10,
        dry_run=True,
    )

    assert result["ok"] is False
    assert result["affected_parents"] == 0
    assert result["candidate_count"] == 0
    assert result["results"][0]["ok"] is False
    assert result["results"][0]["reason"] == "invalid_source_html_record_cleanup_result"


def test_cleanup_source_html_records_checks_lease_before_each_trash(
    tmp_path: Path,
) -> None:
    guard_calls: list[str] = []
    trash_calls: list[str] = []

    def ensure_active() -> None:
        guard_calls.append("check")
        if len(guard_calls) >= 2:
            raise RuntimeError("metadata lease lost")

    def trash_attachment(**kwargs: Any) -> dict[str, Any]:
        trash_calls.append(kwargs["attachment"].key)
        return {"ok": True, "trashed": True}

    with pytest.raises(RuntimeError, match="metadata lease lost"):
        cleanup_source_html_records(
            metadata=SimpleNamespace(
                library_id="LIB1",
                data_dir=tmp_path,
                key="ITEM1234",
                item_id=10,
                title="Article",
            ),
            records=[
                FullTextAttachmentRecord(
                    key="HTMLMISS1",
                    content_type="text/html",
                    path="storage:Article 1 [SOURCE HTML].html",
                    title="Article 1 [source HTML]",
                    file_path=str(tmp_path / "storage" / "HTMLMISS1" / "missing.html"),
                    exists=False,
                ),
                FullTextAttachmentRecord(
                    key="HTMLMISS2",
                    content_type="text/html",
                    path="storage:Article 2 [SOURCE HTML].html",
                    title="Article 2 [source HTML]",
                    file_path=str(tmp_path / "storage" / "HTMLMISS2" / "missing.html"),
                    exists=False,
                ),
            ],
            storage_dir=tmp_path / "storage",
            trash_attachment=trash_attachment,
            dry_run=False,
            ensure_active=ensure_active,
        )

    assert guard_calls == ["check", "check"]
    assert trash_calls == ["HTMLMISS1"]
