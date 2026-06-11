from __future__ import annotations

from pathlib import Path


HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml", "multipart/related", "message/rfc822"})
PDF_CONTENT_TYPES = frozenset({"application/pdf", "application/x-pdf"})
HTML_SUFFIXES = frozenset({".html", ".htm", ".mhtml", ".mht"})
PDF_SUFFIXES = frozenset({".pdf"})


def is_pdf_attachment(
    *,
    content_type: str | None = None,
    path: str | None = None,
    file_path: str | None = None,
) -> bool:
    mime = base_content_type(content_type)
    if mime in HTML_CONTENT_TYPES:
        return False
    if mime in PDF_CONTENT_TYPES:
        return True
    return has_path_suffix(path, PDF_SUFFIXES) or has_path_suffix(file_path, PDF_SUFFIXES)


def is_html_attachment(
    *,
    content_type: str | None = None,
    path: str | None = None,
    file_path: str | None = None,
) -> bool:
    mime = base_content_type(content_type)
    if mime in PDF_CONTENT_TYPES:
        return False
    return (
        mime in HTML_CONTENT_TYPES
        or has_path_suffix(path, HTML_SUFFIXES)
        or has_path_suffix(file_path, HTML_SUFFIXES)
    )


def base_content_type(value: str | None) -> str:
    return str(value or "").split(";", 1)[0].strip().casefold()


def has_path_suffix(value: str | None, suffixes: frozenset[str]) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if text.startswith("storage:"):
        text = text.removeprefix("storage:")
    text = text.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return Path(text).suffix.casefold() in suffixes
