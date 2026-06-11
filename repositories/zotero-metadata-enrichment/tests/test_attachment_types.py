from __future__ import annotations

from zotero_metadata_enrichment.attachment_types import is_html_attachment, is_pdf_attachment


def test_attachment_type_classifier_does_not_treat_pdf_html_as_pdf() -> None:
    assert is_pdf_attachment(content_type="text/html", path="storage:paper.pdf.html") is False
    assert is_html_attachment(content_type="text/html", path="storage:paper.pdf.html") is True


def test_attachment_type_classifier_normalizes_mime_values() -> None:
    assert is_pdf_attachment(content_type="Application/PDF", path="storage:download") is True
    assert is_html_attachment(content_type="Text/HTML; charset=UTF-8", path="storage:paper.pdf") is True
    assert is_pdf_attachment(content_type="Text/HTML; charset=UTF-8", path="storage:paper.pdf") is False
