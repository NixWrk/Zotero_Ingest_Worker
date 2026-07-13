from __future__ import annotations

import json

from zotero_ingest_worker.local_zotero_paths import (
    library_id_for_data_dir,
    looks_like_generated_html,
    path_library_id_for_data_dir,
    resolve_attachment_path_for_suffixes,
)


def test_looks_like_generated_html_accepts_language_suffixes() -> None:
    assert looks_like_generated_html("Paper [EN HTML].html")
    assert looks_like_generated_html("Paper [RU HTML].html")
    assert looks_like_generated_html("Paper [UNKNOWN HTML].HTML")
    assert not looks_like_generated_html("Paper.html")
    assert not looks_like_generated_html("Paper [OCR].html")
    assert not looks_like_generated_html("Paper [EN HTML].pdf")


def test_resolve_attachment_path_for_storage_path(tmp_path) -> None:
    storage_root = tmp_path / "storage"
    target = storage_root / "PDF1234" / "paper.pdf"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"%PDF")

    resolved = resolve_attachment_path_for_suffixes(
        storage_root=storage_root,
        key="PDF1234",
        zotero_path="storage:paper.pdf",
        suffixes=(".pdf",),
        require_exists=True,
    )

    assert resolved == target


def test_library_id_prefers_matching_relay_binding(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(
        "ZFR_LIBRARY_BINDINGS",
        json.dumps([{"zoteroLibraryId": "19686658", "dataDir": str(tmp_path)}]),
    )

    assert library_id_for_data_dir(tmp_path) == "19686658"
    assert path_library_id_for_data_dir(tmp_path) != "19686658"
