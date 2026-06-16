"""IOPscience article polish rules."""

from __future__ import annotations

from html import escape as html_escape
from html import unescape
import re

from .core import (
    WebArticleExtraction,
    WebHtmlKind,
    _attr_value,
    _balanced_element_from_match,
    _extract_fragment_by_attr_tokens,
    _remove_elements_by_attr_tokens,
    _set_attr_value,
    _visible_text_length,
    extract_generic_web_article_fragment,
)


_IMG_OPEN_RE = re.compile(r"<img\b(?P<attrs>[^>]*)>", re.IGNORECASE | re.DOTALL)
_FA_ICON_SVG_RE = re.compile(
    r"<svg\b(?=[^>]*\bclass\s*=\s*(['\"])[^'\"]*\bfa-icon\b)[\s\S]*?</svg>",
    re.IGNORECASE,
)
_EXPORT_CITATION_RE = re.compile(
    r"<p\b[^>]*>\s*<small>\s*Export\s+citation\s+and\s+abstract\s*</small>\s*</p>",
    re.IGNORECASE,
)
_REFERENCES_CONTAINER_RE = re.compile(
    r"<(?P<tag>div|section)\b(?P<attrs>[^<>]*\breferences\b[^<>]*)>",
    re.IGNORECASE,
)
_META_TAG_RE = re.compile(r"<meta\b(?P<attrs>[^>]*)>", re.IGNORECASE | re.DOTALL)
_IOP_BIB_HREF_RE = re.compile(
    r"(?<![\w:-])href\s*=\s*(['\"])#(?P<base>[A-Za-z0-9_.:-]*bib)(?P<number>\d+)\1",
    re.IGNORECASE,
)
_TEX_SPAN_RE = re.compile(
    r"<span\b(?=[^>]*\bclass\s*=\s*(['\"])[^'\"]*\btex\b)[^>]*>\s*"
    r"(?:<span\b(?=[^>]*\bclass\s*=\s*(['\"])[^'\"]*\btexImage\b)[\s\S]*?</span>\s*)?"
    r"<script\b(?P<script_attrs>[^>]*)>(?P<tex>[\s\S]*?)</script>\s*"
    r"</span>",
    re.IGNORECASE,
)
_MATH_TEX_SCRIPT_RE = re.compile(
    r"<script\b(?P<script_attrs>[^>]*)>(?P<tex>[\s\S]*?)</script>",
    re.IGNORECASE,
)


def extract_article_fragment(html: str) -> WebArticleExtraction:
    source_html = html
    html = _replace_iop_tex_fallbacks(html)
    extraction = _extract_fragment_by_attr_tokens(
        html,
        kind=WebHtmlKind.IOP_ARTICLE,
        token_selectors=(
            ("article-content", ".article-content"),
        ),
        min_text_length=800,
    )
    if extraction is not None:
        return WebArticleExtraction(
            html=_append_meta_references(extraction.html, source_html),
            extracted=extraction.extracted,
            selector=extraction.selector,
            text_length=extraction.text_length,
        )

    extraction = _extract_fragment_by_attr_tokens(
        html,
        kind=WebHtmlKind.IOP_ARTICLE,
        token_selectors=(
            ("wd-jnl-art-full-text", ".wd-jnl-art-full-text"),
            ("itemprop=\"articleBody\"", '[itemprop="articleBody"]'),
            ("itemprop='articleBody'", '[itemprop="articleBody"]'),
        ),
        min_text_length=800,
    )
    extraction = extraction or extract_generic_web_article_fragment(html, kind=WebHtmlKind.IOP_ARTICLE)
    return WebArticleExtraction(
        html=_append_meta_references(extraction.html, source_html),
        extracted=extraction.extracted,
        selector=extraction.selector,
        text_length=extraction.text_length,
    )


def normalize_article_fragment(
    html: str,
    *,
    source_url: str | None = None,
    canonical_url: str | None = None,
) -> str:
    del source_url, canonical_url
    html = _promote_lazy_image_sources(html)
    html = _remove_elements_by_attr_tokens(
        html,
        (
            "leaderboard-ad",
            "ad-iframe",
            "ad-iframe-wrap",
            "advert",
            "middle-ad",
            "sidebar-ad",
            "article-metrics",
            "wd-jnl-art-license",
            "jnl-art-license",
            "content-tools",
            "article-tools",
            "content-nav",
            "linked-articles",
            "related-content",
            "related-article",
            "wd-related-articles",
            "side-and-below",
            "recommend",
            "download-options",
            "fig-dwnld",
            "btn-multi-block",
            "zoom-tools",
            "reveal-trigger",
            "loading-icon",
            "print-hide",
            "share",
            "cookie",
            "toolbar",
        ),
        tags=("aside", "button", "div", "footer", "header", "nav", "p", "section", "span"),
    )
    html = _EXPORT_CITATION_RE.sub(" ", html)
    html = _FA_ICON_SVG_RE.sub(" ", html)
    html = _remove_empty_references_shells(html)
    return html.strip()


def _promote_lazy_image_sources(html: str) -> str:
    def replace(match: re.Match[str]) -> str:
        open_tag = match.group(0)
        attrs = match.group("attrs")
        data_src = (_attr_value(attrs, "data-src") or "").strip()
        if not _is_remote_url(data_src):
            return open_tag

        src = (_attr_value(attrs, "src") or "").strip()
        if src and not _is_placeholder_src(src):
            return open_tag

        next_tag = _set_attr_value(open_tag, "src", data_src)
        if "data-z2m-src-placeholder" in next_tag.lower() or not src:
            return next_tag
        escaped_src = html_escape(src, quote=True)
        return re.sub(r">\s*$", f' data-z2m-src-placeholder="{escaped_src}">', next_tag, count=1)

    return _IMG_OPEN_RE.sub(replace, html)


def _is_remote_url(value: str) -> bool:
    lowered = unescape(value).strip().lower()
    return lowered.startswith("https://") or lowered.startswith("http://")


def _is_placeholder_src(value: str) -> bool:
    lowered = unescape(value).strip().lower()
    return lowered.startswith("data:image/")


def _remove_empty_references_shells(html: str) -> str:
    cleaned = html
    previous = None
    while previous != cleaned:
        previous = cleaned
        for match in list(_REFERENCES_CONTAINER_RE.finditer(cleaned)):
            fragment = _balanced_element_from_match(cleaned, match)
            if fragment is None:
                continue
            visible_length = _visible_text_length(fragment)
            if visible_length >= 20:
                continue
            cleaned = cleaned[: match.start()] + " " + cleaned[match.start() + len(fragment) :]
            break
    return cleaned


def _replace_iop_tex_fallbacks(html: str) -> str:
    html = _TEX_SPAN_RE.sub(lambda match: _tex_replacement(match.group("script_attrs"), match.group("tex")), html)
    return _MATH_TEX_SCRIPT_RE.sub(
        lambda match: _tex_replacement(match.group("script_attrs"), match.group("tex")),
        html,
    )


def _tex_replacement(script_attrs: str, tex: str) -> str:
    type_value = (_attr_value(script_attrs, "type") or "").lower()
    if not type_value.startswith("math/tex"):
        return f"<script{script_attrs}>{tex}</script>"
    display = "mode=display" in type_value
    normalized = _normalize_tex_source(tex)
    if not normalized:
        return ""
    if display:
        return f"\\[{html_escape(normalized, quote=False)}\\]"
    return f"\\({html_escape(normalized, quote=False)}\\)"


def _normalize_tex_source(tex: str) -> str:
    return re.sub(r"\s+", " ", unescape(tex)).strip()


def _append_meta_references(article_html: str, full_html: str) -> str:
    references = _citation_reference_values(full_html)
    if not references:
        return article_html

    bib_base = _iop_bib_id_base(article_html)
    if _has_reference_targets(article_html, bib_base, len(references)):
        return article_html

    items: list[str] = []
    for index, reference in enumerate(references, start=1):
        rendered = _render_reference(reference)
        if not rendered:
            continue
        items.append(f'<li id="{html_escape(f"{bib_base}{index}", quote=True)}">{rendered}</li>')
    if not items:
        return article_html

    section = (
        '<section class="z2m-iop-references" id="z2m-iop-references">'
        "<h2>References</h2>"
        f"<ol>{''.join(items)}</ol>"
        "</section>"
    )
    return f"{article_html.rstrip()}\n{section}"


def _citation_reference_values(html: str) -> list[str]:
    references: list[str] = []
    for match in _META_TAG_RE.finditer(html):
        attrs = match.group("attrs") or ""
        name = (_attr_value(attrs, "name") or "").strip().lower()
        if name != "citation_reference":
            continue
        content = (_attr_value(attrs, "content") or "").strip()
        if content:
            references.append(content)
    return references


def _iop_bib_id_base(html: str) -> str:
    counts: dict[str, int] = {}
    for match in _IOP_BIB_HREF_RE.finditer(html):
        base = match.group("base")
        counts[base] = counts.get(base, 0) + 1
    if not counts:
        return "iopbib"
    return max(counts.items(), key=lambda item: item[1])[0]


def _has_reference_targets(html: str, bib_base: str, reference_count: int) -> bool:
    sample_count = min(max(reference_count, 1), 3)
    for index in range(1, sample_count + 1):
        target = re.escape(f"{bib_base}{index}")
        if re.search(rf"(?<![\w:-])(?:id|name)\s*=\s*(['\"]){target}\1", html, flags=re.IGNORECASE):
            return True
    return False


def _render_reference(reference: str) -> str:
    fields = _parse_reference_fields(reference)
    authors = fields.get("citation_author", [])
    title = _first(fields, "citation_title")
    container = (
        _first(fields, "citation_journal_title")
        or _first(fields, "citation_conference_title")
        or _first(fields, "citation_book_title")
        or _first(fields, "citation_publisher")
    )
    year = _first(fields, "citation_publication_date")
    volume = _first(fields, "citation_volume")
    first_page = _first(fields, "citation_firstpage")
    last_page = _first(fields, "citation_lastpage")
    doi = _first(fields, "citation_doi")

    chunks: list[str] = []
    if authors:
        chunks.append(_reference_authors(authors))
    if year:
        chunks.append(html_escape(year, quote=False))
    if title:
        chunks.append(html_escape(title, quote=False))
    if container:
        chunks.append(f"<em>{html_escape(container, quote=False)}</em>")
    if volume:
        chunks.append(html_escape(volume, quote=False))
    pages = _reference_pages(first_page, last_page)
    if pages:
        chunks.append(html_escape(pages, quote=False))
    if doi:
        escaped_doi = html_escape(doi, quote=False)
        escaped_href = html_escape(f"https://doi.org/{doi}", quote=True)
        chunks.append(f'<a href="{escaped_href}">{escaped_doi}</a>')
    if not chunks:
        return html_escape(reference, quote=False)
    return ". ".join(chunks) + "."


def _parse_reference_fields(reference: str) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    for part in reference.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            continue
        fields.setdefault(key, []).append(value)
    return fields


def _first(fields: dict[str, list[str]], key: str) -> str:
    values = fields.get(key) or []
    return values[0] if values else ""


def _reference_authors(authors: list[str]) -> str:
    if len(authors) <= 6:
        return html_escape(", ".join(authors), quote=False)
    shown = ", ".join(authors[:6])
    return f"{html_escape(shown, quote=False)}, et al"


def _reference_pages(first_page: str, last_page: str) -> str:
    if first_page and last_page:
        return f"{first_page}-{last_page}"
    return first_page or last_page
