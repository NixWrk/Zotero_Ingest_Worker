"""Springer/Nature Link article polish rules."""

from __future__ import annotations

from collections.abc import Callable
from html import escape as html_escape
from html import unescape
import re
import urllib.parse

from ..html_links import ATTR_HREF_RE
from .core import (
    _attr_value,
    _balanced_element_from_match,
    WebArticleExtraction,
    WebHtmlKind,
    _extract_fragment_by_attr_tokens,
    _remove_elements_by_attr_tokens,
    extract_generic_web_article_fragment,
)


RemoteHtmlFetcher = Callable[[str], str]

_SPRINGER_FLOAT_OPEN_RE = re.compile(
    r"<(?P<tag>div|section|figure)\b(?P<attrs>[^<>]*)>",
    re.IGNORECASE | re.DOTALL,
)
_SPRINGER_FLOAT_LABEL_ID_RE = re.compile(
    r"(?<![\w:-])id\s*=\s*(['\"])(?P<id>(?:Fig|Tab|Table)\d[A-Za-z0-9:._-]*)\1",
    re.IGNORECASE | re.DOTALL,
)
_SPRINGER_A_OPEN_RE = re.compile(r"<a\b(?P<attrs>[^>]*)>", re.IGNORECASE | re.DOTALL)
_SPRINGER_TABLE_CONTAINER_OPEN_RE = re.compile(
    r"<(?P<tag>div)\b(?P<attrs>[^<>]*)>",
    re.IGNORECASE | re.DOTALL,
)
_SPRINGER_TABLE_RE = re.compile(r"<table\b[\s\S]*?</table>", re.IGNORECASE)
_SPRINGER_FULL_SIZE_TABLE_BUTTON_RE = re.compile(
    r"<div\b(?=[^>]*\bu-text-right\b)(?=[^>]*\bu-hide-print\b)[^>]*>\s*"
    r"<a\b(?=[^>]*\bdata-test\s*=\s*(['\"])table-link\1)[\s\S]*?</a>\s*</div>",
    re.IGNORECASE,
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
    fetch_text: RemoteHtmlFetcher | None = None,
) -> str:
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
    html = _inline_full_size_tables(
        html,
        base_url=canonical_url or source_url,
        fetch_text=fetch_text,
    )
    html = _retarget_float_label_links_to_containers(html)
    return html.strip()


def _inline_full_size_tables(
    html: str,
    *,
    base_url: str | None,
    fetch_text: RemoteHtmlFetcher | None,
) -> str:
    if fetch_text is None:
        return html

    cache: dict[str, str | None] = {}
    replacements: list[tuple[int, int, str]] = []
    for match in _SPRINGER_FLOAT_OPEN_RE.finditer(html):
        attrs = match.group("attrs") or ""
        if _springer_float_container_kind(attrs) != "table":
            continue
        fragment = _balanced_element_from_match(html, match)
        if fragment is None or "<table" in fragment.lower():
            continue
        table_page_url = _springer_table_page_url(fragment, base_url=base_url)
        if table_page_url is None:
            continue
        table_html = cache.get(table_page_url)
        if table_page_url not in cache:
            table_html = _fetch_springer_table_payload(table_page_url, fetch_text=fetch_text)
            cache[table_page_url] = table_html
        if table_html is None:
            continue
        replacements.append(
            (
                match.start(),
                match.start() + len(fragment),
                _merge_springer_table_payload(fragment, table_html),
            )
        )

    for start, end, replacement in reversed(replacements):
        html = html[:start] + replacement + html[end:]
    return html


def _springer_table_page_url(fragment: str, *, base_url: str | None) -> str | None:
    for match in _SPRINGER_A_OPEN_RE.finditer(fragment):
        attrs = match.group("attrs") or ""
        href = (_attr_value(attrs, "href") or "").strip()
        if not href:
            continue
        resolved = urllib.parse.urljoin(base_url or "", href)
        parsed = urllib.parse.urlsplit(resolved)
        if parsed.scheme and parsed.scheme.lower() not in {"http", "https"}:
            continue
        if not re.search(r"/tables/\d+/?$", urllib.parse.unquote(parsed.path), re.IGNORECASE):
            continue
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
    return None


def _fetch_springer_table_payload(
    table_page_url: str,
    *,
    fetch_text: RemoteHtmlFetcher,
) -> str | None:
    try:
        table_page_html = fetch_text(table_page_url)
    except Exception:
        return None
    return _extract_springer_table_payload(table_page_html)


def _extract_springer_table_payload(table_page_html: str) -> str | None:
    for match in _SPRINGER_TABLE_CONTAINER_OPEN_RE.finditer(table_page_html):
        attrs = unescape(match.group("attrs") or "").lower()
        if "c-article-table-container" not in attrs:
            continue
        fragment = _balanced_element_from_match(table_page_html, match)
        if fragment is not None and "<table" in fragment.lower():
            return fragment.strip()

    table_match = _SPRINGER_TABLE_RE.search(table_page_html)
    if table_match is None:
        return None
    return f'<div class="c-article-table-container">{table_match.group(0)}</div>'


def _merge_springer_table_payload(placeholder_html: str, table_html: str) -> str:
    cleaned = _SPRINGER_FULL_SIZE_TABLE_BUTTON_RE.sub(" ", placeholder_html)
    if "<table" in cleaned.lower():
        return cleaned
    if re.search(r"</figcaption\s*>", cleaned, flags=re.IGNORECASE):
        return re.sub(
            r"</figcaption\s*>",
            lambda match: f"{match.group(0)}{table_html}",
            cleaned,
            count=1,
            flags=re.IGNORECASE,
        )
    if re.search(r"</figure\s*>", cleaned, flags=re.IGNORECASE):
        return re.sub(
            r"</figure\s*>",
            f"{table_html}</figure>",
            cleaned,
            count=1,
            flags=re.IGNORECASE,
        )
    return f"{cleaned}{table_html}"


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
