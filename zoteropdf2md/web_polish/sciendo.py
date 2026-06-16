"""Sciendo/Reference Global routing helpers."""

from __future__ import annotations

from .core import WebHtmlPolishError


def raise_not_full_text() -> None:
    raise WebHtmlPolishError(
        "Sciendo abstract-tab HTML is not full text; rediscover/fetch citation_full_html_url with ?tab=article."
    )
