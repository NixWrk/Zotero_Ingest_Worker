from __future__ import annotations

from zotero_ingest_worker.local_zotero_paths import (
    looks_like_generated_html,
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
