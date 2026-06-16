"""PMC/NCBI article polish rules."""

from __future__ import annotations

import re
import urllib.parse

from ..html_links import extract_html_fragment_targets
from .core import (
    WebArticleExtraction,
    WebHtmlKind,
    _attr_value,
    _extract_fragment_by_attr_tokens,
    _remove_elements_by_attr_tokens,
    _set_attr_value,
    extract_generic_web_article_fragment,
)


_A_OPEN_RE = re.compile(r"<a\b(?P<attrs>[^>]*)>", re.IGNORECASE | re.DOTALL)
_FIGURE_PATH_RE = re.compile(r"(?:^|/)figure/(?P<label>fig(?:ure)?[-_]?0*(?P<num>\d+))/?$", re.IGNORECASE)
_TABLE_PATH_RE = re.compile(r"(?:^|/)table/(?P<label>tab(?:le)?[-_]?0*(?P<num>\d+)|tbl[-_]?0*(?P<num2>\d+))/?$", re.IGNORECASE)


def extract_article_fragment(html: str) -> WebArticleExtraction:
    extraction = _extract_fragment_by_attr_tokens(
        html,
        kind=WebHtmlKind.PMC_ARTICLE,
        token_selectors=(
            ("pmc-article", ".pmc-article"),
            ("pmc-article-section", ".pmc-article-section"),
            ("main-content", "#main-content"),
        ),
        min_text_length=800,
    )
    return extraction or extract_generic_web_article_fragment(html, kind=WebHtmlKind.PMC_ARTICLE)


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
            "pmc-actions-bar",
            "pmc-sidebar",
            "pmc-search",
            "pmc-page-banner",
            "pmc-menu",
            "article-page-sidebar",
            "usa-banner",
            "usa-overlay",
            "social-sharing",
            "figpopup",
            "ncbi-header",
            "ncbi-footer",
        ),
    )
    return _rewrite_float_path_links(html).strip()


def _rewrite_float_path_links(html: str) -> str:
    ids = extract_html_fragment_targets(html)
    if not ids:
        return html

    def replace(match: re.Match[str]) -> str:
        open_tag = match.group(0)
        attrs = match.group("attrs")
        href = (_attr_value(attrs, "href") or "").strip()
        if not href or href.startswith("#"):
            return open_tag
        target = _float_target_for_href(href, ids)
        if target is None:
            return open_tag
        return _set_attr_value(open_tag, "href", f"#{target}")

    return _A_OPEN_RE.sub(replace, html)


def _float_target_for_href(href: str, ids: set[str]) -> str | None:
    parsed = urllib.parse.urlsplit(href)
    path = urllib.parse.unquote(parsed.path)
    figure_match = _FIGURE_PATH_RE.search(path)
    if figure_match is not None:
        return _resolve_float_target(
            ids,
            label=figure_match.group("label"),
            number=figure_match.group("num"),
            candidates_prefixes=("FIG", "Fig", "fig", "F", "f", "figure", "Figure"),
        )

    table_match = _TABLE_PATH_RE.search(path)
    if table_match is not None:
        number = table_match.group("num") or table_match.group("num2")
        return _resolve_float_target(
            ids,
            label=table_match.group("label"),
            number=number,
            candidates_prefixes=("T", "t", "TAB", "Tab", "tab", "TABLE", "Table", "table", "tbl"),
        )
    return None


def _resolve_float_target(
    ids: set[str],
    *,
    label: str,
    number: str,
    candidates_prefixes: tuple[str, ...],
) -> str | None:
    lower_to_id = {item.lower(): item for item in ids}
    candidates = [label]
    candidates.extend(f"{prefix}{number}" for prefix in candidates_prefixes)
    for candidate in candidates:
        if candidate in ids:
            return candidate
        resolved = lower_to_id.get(candidate.lower())
        if resolved:
            return resolved
    return None
