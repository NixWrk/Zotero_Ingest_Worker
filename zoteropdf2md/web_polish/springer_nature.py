"""Springer/Nature Link article polish rules."""

from __future__ import annotations

from html import escape as html_escape
from html import unescape
import re
import urllib.parse

from ..html_links import ATTR_HREF_RE
from .core import (
    _attr_value,
    WebArticleExtraction,
    WebHtmlKind,
    _extract_fragment_by_attr_tokens,
    _remove_elements_by_attr_tokens,
    extract_generic_web_article_fragment,
)


_SPRINGER_FLOAT_OPEN_RE = re.compile(
    r"<(?P<tag>div|section|figure)\b(?P<attrs>[^<>]*)>",
    re.IGNORECASE | re.DOTALL,
)
_SPRINGER_FLOAT_LABEL_ID_RE = re.compile(
    r"(?<![\w:-])id\s*=\s*(['\"])(?P<id>(?:Fig|Tab|Table)\d[A-Za-z0-9:._-]*)\1",
    re.IGNORECASE | re.DOTALL,
)


def extract_article_fragment(html: str) -> WebArticleExtraction:
    extraction = _extract_fragment_by_attr_tokens(
        html,
        kind=WebHtmlKind.SPRINGER_NATURE_ARTICLE,
        token_selectors=(
            ("c-article-main", ".c-article-main"),
            ("c-article-body", ".c-article-body"),
            ("article__body", ".article__body"),
            ("article-body", ".article-body"),
            ("main-content", "#main-content"),
        ),
        min_text_length=800,
    )
    return extraction or extract_generic_web_article_fragment(html, kind=WebHtmlKind.SPRINGER_NATURE_ARTICLE)


def normalize_article_fragment(
    html: str,
    *,
    source_url: str | None = None,
    canonical_url: str | None = None,
) -> str:
    del source_url, canonical_url
    html = _remove_elements_by_attr_tokens(
        html,
        (
            "c-article-extras",
            "c-article-metrics",
            "c-article-recommendations",
            "c-article-sidebar",
            "c-article-related",
            "c-pdf-download",
            "app-article-metrics",
            "js-article__aside",
            "js-article-sidebar",
            "article-sidebar",
            "share",
            "advert",
        ),
    )
    html = _retarget_float_label_links_to_containers(html)
    return html.strip()


def _retarget_float_label_links_to_containers(html: str) -> str:
    label_to_container = _springer_float_label_target_map(html)
    if not label_to_container:
        return html

    def replace_href(match: re.Match[str]) -> str:
        quote = match.group("quote")
        href = unescape(match.group("href")).strip()
        if "#" not in href:
            return match.group(0)
        target = urllib.parse.unquote(href.rsplit("#", 1)[1])
        container_id = label_to_container.get(target)
        if container_id is None:
            return match.group(0)
        local_href = html_escape(f"#{container_id}", quote=True)
        return f"{match.group('prefix')}{quote}{local_href}{quote}"

    return ATTR_HREF_RE.sub(replace_href, html)


def _springer_float_label_target_map(html: str) -> dict[str, str]:
    label_to_container: dict[str, str] = {}
    for match in _SPRINGER_FLOAT_OPEN_RE.finditer(html):
        attrs = match.group("attrs") or ""
        container_kind = _springer_float_container_kind(attrs)
        if container_kind is None:
            continue
        container_id = _attr_value(attrs, "id")
        if not container_id:
            continue
        window = html[match.end() : match.end() + 6000]
        prefixes = ("fig",) if container_kind == "figure" else ("tab", "table")
        for label_match in _SPRINGER_FLOAT_LABEL_ID_RE.finditer(window):
            label_id = unescape(label_match.group("id")).strip()
            if label_id == container_id:
                continue
            if label_id.lower().startswith(prefixes):
                label_to_container.setdefault(label_id, container_id)
                break
    return label_to_container


def _springer_float_container_kind(attrs: str) -> str | None:
    lowered_attrs = unescape(attrs).lower()
    if (
        "c-article-section__figure" in lowered_attrs
        or "data-container-section=\"figure\"" in lowered_attrs
        or "data-container-section='figure'" in lowered_attrs
    ):
        return "figure"
    if (
        "c-article-section__table" in lowered_attrs
        or "c-article-table" in lowered_attrs
        or "data-container-section=\"table\"" in lowered_attrs
        or "data-container-section='table'" in lowered_attrs
    ):
        return "table"
    return None
