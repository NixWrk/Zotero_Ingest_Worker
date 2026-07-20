from __future__ import annotations

import pytest

from zotero_ingest_worker.full_text_inventory import (
    FullTextAttachmentRecord,
    FullTextInventory,
    inventory_has_pdf,
    inventory_has_source_html,
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


def test_full_text_inventory_treats_legacy_non_ru_html_as_source() -> None:
    inventory = FullTextInventory(
        (
            FullTextAttachmentRecord(
                key="PDF1234",
                content_type="application/pdf",
                path="storage:paper.pdf",
                exists=True,
            ),
            FullTextAttachmentRecord(
                key="HTMLZH",
                content_type="text/html",
                path="storage:Article [ZH HTML].html",
                exists=True,
            ),
            FullTextAttachmentRecord(
                key="HTMLRU",
                content_type="text/html",
                path="storage:Article [RU HTML].html",
                exists=True,
            ),
            FullTextAttachmentRecord(
                key="ARXIV1",
                content_type="text/html",
                path="storage:Article [ARXIV HTML].html",
                exists=True,
            ),
        )
    )

    attachments = {item["key"]: item for item in inventory.to_dict()["attachments"]}

    assert inventory.source_html_count == 1
    assert attachments["HTMLZH"]["is_source_html"] is True
    assert attachments["HTMLRU"]["is_source_html"] is False
    assert attachments["ARXIV1"]["is_source_html"] is False
    assert should_skip_full_text_scan(inventory) is True


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


@pytest.mark.parametrize(
    "invalid_flag",
    ["false", 1],
    ids=["string-false", "integer-one"],
)
def test_inventory_flags_require_exact_booleans(invalid_flag: object) -> None:
    inventory: dict[str, object] = {
        "has_pdf": invalid_flag,
        "has_source_html": invalid_flag,
        "has_html": invalid_flag,
        "pdf_count": 0,
        "source_html_count": 0,
        "html_count": 0,
    }

    assert inventory_has_pdf(inventory) is False
    assert inventory_has_source_html(inventory) is False
    assert should_skip_full_text_scan(inventory) is False
    assert pdf_download_limit(inventory) == 3
    assert inventory_fingerprint(inventory) == ("pdf=0:0|source_html=0:0|html=0:0")


@pytest.mark.parametrize(
    "invalid_count",
    [True, "1", 1.5, -1],
    ids=["boolean", "numeric-string", "float", "negative"],
)
def test_inventory_fingerprint_requires_exact_nonnegative_counts(
    invalid_count: object,
) -> None:
    inventory: dict[str, object] = {
        "has_pdf": False,
        "has_source_html": False,
        "has_html": False,
        "pdf_count": invalid_count,
        "source_html_count": invalid_count,
        "html_count": invalid_count,
    }

    assert inventory_fingerprint(inventory) == ("pdf=0:0|source_html=0:0|html=0:0")
