"""arXiv LaTeXML polish rules."""

from __future__ import annotations

from .core import (
    WebArticleExtraction,
    WebHtmlKind,
    _extract_fragment_by_attr_tokens,
    _remove_elements_by_attr_tokens,
    extract_generic_web_article_fragment,
)


def extract_article_fragment(html: str) -> WebArticleExtraction:
    extraction = _extract_fragment_by_attr_tokens(
        html,
        kind=WebHtmlKind.ARXIV_LATEXML,
        token_selectors=(
            ("ltx_page_main", ".ltx_page_main"),
            ("ltx_document", ".ltx_document"),
        ),
        min_text_length=500,
    )
    return extraction or extract_generic_web_article_fragment(html, kind=WebHtmlKind.ARXIV_LATEXML)


def normalize_article_fragment(
    html: str,
    *,
    source_url: str | None = None,
    canonical_url: str | None = None,
) -> str:
    del source_url, canonical_url
    return _remove_elements_by_attr_tokens(
        html,
        (
            "ltx_page_navbar",
            "ltx_page_header",
            "ltx_page_footer",
            "ltx_page_logo",
            "ltx_page_sidebar",
        ),
    ).strip()
