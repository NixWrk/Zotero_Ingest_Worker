"""Publisher-specific polish helpers for web-native article HTML."""

from __future__ import annotations

from .core import WebArticleExtraction, WebHtmlKind, WebHtmlPolishError
from .registry import (
    WebPolishHandler,
    default_origin_for_kind,
    handler_for_kind,
    registered_web_polish_handlers,
)

__all__ = [
    "WebArticleExtraction",
    "WebHtmlKind",
    "WebHtmlPolishError",
    "WebPolishHandler",
    "default_origin_for_kind",
    "handler_for_kind",
    "registered_web_polish_handlers",
]
