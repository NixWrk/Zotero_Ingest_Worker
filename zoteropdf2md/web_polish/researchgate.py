"""ResearchGate routing helpers.

ResearchGate pages discovered as HTML are usually landing/PDF pages rather than
stable full-text article HTML, so the web polish route rejects them by design.
"""

from __future__ import annotations

from .core import WebHtmlPolishError


def raise_not_full_text() -> None:
    raise WebHtmlPolishError(
        "ResearchGate HTML is a landing/PDF page; route to PDF conversion instead of web-html-polish."
    )
