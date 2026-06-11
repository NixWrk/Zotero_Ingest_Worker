from __future__ import annotations

from zotero_ingest_worker.full_text_inventory import (
    FullTextAttachmentRecord,
    FullTextInventory,
    inventory_fingerprint,
    pdf_download_limit,
    should_skip_full_text_scan,
)


def test_full_text_inventory_distinguishes_source_html_from_other_html() -> None:
    inventory = FullTextInventory(
        (
            FullTextAttachmentRecord(
                key="PDF1234",
                content_type="application/pdf",
                path="storage:paper.pdf",
                exists=True,
            ),
            FullTextAttachmentRecord(
                key="HTML1234",
                content_type="text/html",
                path="storage:publisher_snapshot.html",
                exists=True,
            ),
        )
    )

    payload = inventory.to_dict()

    assert payload["has_pdf"] is True
    assert payload["has_html"] is True
    assert payload["has_source_html"] is False
    assert should_skip_full_text_scan(inventory) is False
    assert pdf_download_limit(inventory) == 0


def test_full_text_inventory_skips_scan_only_when_pdf_and_source_html_exist() -> None:
    inventory = FullTextInventory(
        (
            FullTextAttachmentRecord(
                key="PDF1234",
                content_type="application/pdf",
                path="storage:paper.pdf",
                exists=True,
            ),
            FullTextAttachmentRecord(
                key="HTML1234",
                content_type="text/html",
                path="storage:Article [SOURCE HTML].html",
                exists=True,
            ),
        )
    )

    assert inventory.has_source_html is True
    assert should_skip_full_text_scan(inventory) is True
    assert "source_html=1:1" in inventory_fingerprint(inventory)


def test_html_file_with_pdf_in_name_does_not_count_as_pdf() -> None:
    inventory = FullTextInventory(
        (
            FullTextAttachmentRecord(
                key="HTML1234",
                content_type="text/html",
                path="storage:Article.pdf [SOURCE HTML].html",
                exists=True,
            ),
        )
    )

    payload = inventory.to_dict()

    assert payload["pdf_count"] == 0
    assert payload["html_count"] == 1
    assert payload["has_pdf"] is False
    assert payload["has_source_html"] is True
    assert should_skip_full_text_scan(inventory) is False
    assert pdf_download_limit(inventory) == 3


def test_pdf_detection_uses_exact_suffix_for_unknown_content_type() -> None:
    pdf = FullTextAttachmentRecord(
        key="PDF1234",
        content_type="",
        path="storage:Paper.PDF",
        exists=True,
    )
    html = FullTextAttachmentRecord(
        key="HTML1234",
        content_type="",
        path="storage:Paper.pdf.html",
        exists=True,
    )

    assert pdf.is_pdf is True
    assert pdf.is_html is False
    assert html.is_pdf is False
    assert html.is_html is True


def test_attachment_content_type_detection_normalizes_mime_values() -> None:
    pdf = FullTextAttachmentRecord(
        key="PDF1234",
        content_type="Application/PDF",
        path="storage:download",
        exists=True,
    )
    html = FullTextAttachmentRecord(
        key="HTML1234",
        content_type="Text/HTML; charset=UTF-8",
        path="storage:paper.pdf",
        exists=True,
    )

    assert pdf.is_pdf is True
    assert pdf.is_html is False
    assert html.is_pdf is False
    assert html.is_html is True
