"""OJS/DOAJ routing helpers."""

from __future__ import annotations

from .core import WebHtmlPolishError


def raise_not_full_text() -> None:
    raise WebHtmlPolishError(
        "OJS/DOAJ HTML is abstract/metadata plus a galley link; route the PDF galley through raw-html-polish."
    )
