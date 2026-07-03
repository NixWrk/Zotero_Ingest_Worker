"""Web-native HTML normalization helpers.

This module is intentionally separate from ``single_file_html``.  The latter
repairs Marker/PDF HTML, while web-native sources such as arXiv LaTeXML mostly
need source-aware normalization.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from html import escape as html_escape
from html import unescape
import mimetypes
from pathlib import Path
import re
import urllib.parse
import urllib.request

from .arxiv_source_recovery import (
    recover_latexml_figures_from_arxiv_source_html,
)
from .html_theme import web_readability_style
from .html_images import (
    IMAGE_SIGNATURES,
    InlineHtmlResult,
    is_inline_or_remote,
    to_data_url,
    validate_data_url,
)
from .raw_html_polish.katex import render_katex_html
from .html_links import (
    _ATTR_HREF_RE,
    _arxiv_abs_parts,
    _arxiv_html_parts,
    _is_root_relative_url,
    _urlsplit_or_none,
    canonicalize_same_document_links,
    count_same_document_absolute_fragment_links,
    declared_document_urls as _declared_document_urls,
    extract_html_fragment_targets,
    is_plain_local_fragment,
)
from .web_polish.core import (
    WebArticleExtraction,
    WebHtmlKind,
    WebHtmlPolishError,
    _HTML_TAG_RE,
    _attr_value,
    _balanced_element_from_match,
    _extract_fragment_by_attr_tokens,
    _remove_elements_by_attr_tokens,
    _set_attr_value,
    _strip_non_article_payloads,
    _visible_text,
    _visible_text_length,
    extract_generic_web_article_fragment,
)
from .web_polish.registry import (
    default_origin_for_kind,
    extract_source_specific_article_fragment,
    normalize_source_specific_article_fragment,
    rejection_message_for_kind,
)


@dataclass(frozen=True)
class WebHtmlPolishResult:
    html: str
    kind: WebHtmlKind
    article_extracted: bool
    article_selector: str | None
    same_document_links_rewritten: int
    unresolved_same_document_links: int


@dataclass(frozen=True)
class WebHtmlFilePolishResult:
    html: str
    kind: WebHtmlKind
    article_extracted: bool
    article_selector: str | None
    same_document_links_rewritten: int
    unresolved_same_document_links: int
    inlined_images: int
    recovered_source_figures: int = 0
    attempted_source_figures: int = 0
    source_recovery_errors: tuple[str, ...] = ()


_IMG_SRC_RE = re.compile(
    r"(?P<prefix><img\b[^>]*?\ssrc\s*=\s*)(?P<quote>['\"])(?P<src>.*?)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)
_SRCSET_RE = re.compile(
    r"(?P<prefix><(?:img|source)\b[^>]*?\ssrcset\s*=\s*)(?P<quote>['\"])(?P<srcset>.*?)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)
_PICTURE_RE = re.compile(
    r"<picture\b(?P<attrs>[^>]*)>(?P<body>[\s\S]*?)</picture>",
    re.IGNORECASE | re.DOTALL,
)
_SOURCE_OPEN_RE = re.compile(r"<source\b[^>]*>", re.IGNORECASE | re.DOTALL)
_IMG_OPEN_RE = re.compile(r"<img\b(?P<attrs>[^>]*)>", re.IGNORECASE | re.DOTALL)
_INLINE_MEDIA_RE = re.compile(r"<(?:img|picture|svg|math|video|canvas|iframe|object|embed)\b", re.IGNORECASE)
_PARAGRAPH_BLOCK_TAGS = frozenset({"ul", "ol", "table", "figure"})
_EMPTY_TABLE_RE = re.compile(r"<table\b[^>]*>\s*</table>", re.IGNORECASE)
_EMPTY_FIGURE_SHELL_RE = re.compile(
    r"<figure\b[^>]*>\s*(?:<div\b[^>]*>\s*<a\b[^>]*>\s*Open\s+in\s+a\s+new\s+tab\s*</a>\s*</div>\s*)?</figure>",
    re.IGNORECASE | re.DOTALL,
)
_LTX_ROWCOLOR_ARTIFACT_RE = re.compile(
    r"<span\b(?=[^>]*\bltx_ERROR\b)[^>]*>\s*\\rowcolor\s*</span>\s*[A-Za-z]+!\d+\s*",
    re.IGNORECASE | re.DOTALL,
)
_LTX_ROWCOLOR_TEXT_RE = re.compile(r"\\rowcolor\s*[A-Za-z]+!\d+\s*", re.IGNORECASE)
_LTX_DESCRIPTION_TEXT_RE = re.compile(r"\\Description\b", re.IGNORECASE)
_CSS_DECLARATION_RE = re.compile(r"(?P<name>[-\w]+)\s*:\s*(?P<value>[^;]+)\s*;?", re.IGNORECASE)
_ATTR_RE_TEMPLATE = r"\s+{name}\s*=\s*(['\"])(?P<value>.*?)\1"
_ROOT_RELATIVE_URL_ATTR_RE = re.compile(
    r"(?P<prefix>(?<![\w:-])(?P<name>href|src|action|poster)\s*=\s*)"
    r"(?P<quote>['\"])(?P<url>.*?)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)
_TITLE_RE = re.compile(r"<title\b[^>]*>(?P<title>[\s\S]*?)</title>", re.IGNORECASE)
_META_TAG_RE = re.compile(r"<meta\b(?P<attrs>[^>]*)>", re.IGNORECASE | re.DOTALL)
_MAX_LOCAL_ASSET_REFERENCE_LENGTH = 4096
_H1_RE = re.compile(r"<h1\b", re.IGNORECASE)
_LTX_EQUATION_ROW_RE = re.compile(r"<tr\b(?P<attrs>[^>]*)>(?P<body>[\s\S]*?)</tr>", re.IGNORECASE)
_TD_OPEN_RE = re.compile(r"<td\b(?P<attrs>[^>]*)>", re.IGNORECASE | re.DOTALL)
_FRONTIERS_REFERENCE_BUTTON_RE = re.compile(
    r"<button\b(?P<attrs>(?=[^>]*\bclass\s*=\s*['\"][^'\"]*\bArticleReference\b)[^>]*)>"
    r"(?P<body>[\s\S]*?)</button>",
    re.IGNORECASE | re.DOTALL,
)
_FRONTIERS_REFERENCE_ANCHOR_RE = re.compile(
    r"<a\b(?P<attrs>(?=[^>]*\bclass\s*=\s*['\"][^'\"]*\bArticleReference\b)[^>]*)>"
    r"(?P<body>[\s\S]*?)</a>",
    re.IGNORECASE | re.DOTALL,
)


def detect_web_html_kind(html: str, *, source_url: str | None = None) -> WebHtmlKind:
    """Classify known web HTML attachments."""

    parsed_source = _urlsplit_or_none(source_url)
    if parsed_source is not None and _arxiv_abs_parts(parsed_source) is not None:
        return WebHtmlKind.ARXIV_ABS_PAGE

    sample = html[:500_000].lower()
    if _looks_like_arxiv_abs_page(sample):
        return WebHtmlKind.ARXIV_ABS_PAGE
    if _looks_like_sciendo_article_page(sample, parsed_source):
        return WebHtmlKind.GENERIC_ARTICLE
    if _looks_like_sciendo_abstract_page(sample, parsed_source):
        return WebHtmlKind.SCIENDO_ABSTRACT_PAGE
    if _looks_like_ojs_abstract_page(sample, parsed_source):
        return WebHtmlKind.OJS_ABSTRACT_PAGE
    if _looks_like_arxiv_latexml(sample, parsed_source):
        return WebHtmlKind.ARXIV_LATEXML
    if _looks_like_pmc_article(sample, parsed_source):
        return WebHtmlKind.PMC_ARTICLE
    if _looks_like_taylor_francis_article(sample, parsed_source):
        return WebHtmlKind.TAYLOR_FRANCIS_ARTICLE
    if _looks_like_springer_nature_article(sample, parsed_source):
        return WebHtmlKind.SPRINGER_NATURE_ARTICLE
    if _looks_like_iop_article(sample, parsed_source):
        return WebHtmlKind.IOP_ARTICLE
    if _looks_like_researchgate_page(sample, parsed_source):
        return WebHtmlKind.RESEARCHGATE_PAGE
    full_sample = html.lower()
    if full_sample != sample:
        if _looks_like_arxiv_abs_page(full_sample):
            return WebHtmlKind.ARXIV_ABS_PAGE
        if _looks_like_sciendo_article_page(full_sample, parsed_source):
            return WebHtmlKind.GENERIC_ARTICLE
        if _looks_like_sciendo_abstract_page(full_sample, parsed_source):
            return WebHtmlKind.SCIENDO_ABSTRACT_PAGE
        if _looks_like_ojs_abstract_page(full_sample, parsed_source):
            return WebHtmlKind.OJS_ABSTRACT_PAGE
        if _looks_like_arxiv_latexml(full_sample, parsed_source):
            return WebHtmlKind.ARXIV_LATEXML
        if _looks_like_pmc_article(full_sample, parsed_source):
            return WebHtmlKind.PMC_ARTICLE
        if _looks_like_taylor_francis_article(full_sample, parsed_source):
            return WebHtmlKind.TAYLOR_FRANCIS_ARTICLE
        if _looks_like_springer_nature_article(full_sample, parsed_source):
            return WebHtmlKind.SPRINGER_NATURE_ARTICLE
        if _looks_like_iop_article(full_sample, parsed_source):
            return WebHtmlKind.IOP_ARTICLE
        if _looks_like_researchgate_page(full_sample, parsed_source):
            return WebHtmlKind.RESEARCHGATE_PAGE
    if "<article" in sample:
        return WebHtmlKind.GENERIC_ARTICLE
    return WebHtmlKind.UNKNOWN


def require_web_article_html(html: str, *, source_url: str | None = None) -> WebHtmlKind:
    """Return the source kind, rejecting known landing pages."""

    kind = detect_web_html_kind(html, source_url=source_url)
    rejection_message = rejection_message_for_kind(kind)
    if rejection_message is not None:
        raise WebHtmlPolishError(rejection_message)
    return kind


def polish_web_html_document(
    html: str,
    *,
    source_url: str | None = None,
    canonical_url: str | None = None,
    fetch_text: "RemoteHtmlFetcher | None" = None,
) -> WebHtmlPolishResult:
    """Normalize a web-native article HTML document.

    This keeps web-native sources separate from the Marker/PDF repair path:
    strip executable payloads, extract the article-like fragment, canonicalize
    same-document links, and wrap the result in a stable readable shell.
    """

    html = unwrap_existing_web_polish_document(html)
    kind = detect_web_html_kind(html, source_url=source_url)
    if kind == WebHtmlKind.SCIENDO_ABSTRACT_PAGE:
        full_text_url = _sciendo_full_text_url(html, source_url=source_url)
        if not full_text_url:
            raise WebHtmlPolishError(rejection_message_for_kind(kind) or "Known non-article web page.")
        html = (fetch_text or _fetch_remote_html)(full_text_url)
        source_url = full_text_url
        canonical_url = canonical_url or full_text_url
        kind = require_web_article_html(html, source_url=source_url)
    else:
        if kind == WebHtmlKind.RESEARCHGATE_PAGE and _looks_like_researchgate_pdf_shell(html):
            raise WebHtmlPolishError(
                "ResearchGate page does not expose article-like HTML; use the PDF attachment when available."
            )
        rejection_message = rejection_message_for_kind(kind)
        if rejection_message is not None:
            raise WebHtmlPolishError(rejection_message)
    title = _document_title(html)
    declared_urls = _declared_document_urls(html)
    inferred_canonical_url = canonical_url or (declared_urls[0] if declared_urls else None)
    extraction = extract_web_article_fragment(html, kind=kind)
    normalized_html = normalize_web_article_fragment(
        extraction.html,
        kind=kind,
        source_url=source_url,
        canonical_url=inferred_canonical_url,
        fetch_text=fetch_text or _fetch_remote_html,
    )
    normalized_html = normalize_frontiers_reference_links(normalized_html)
    normalized_html = remove_publisher_ui_fragments(normalized_html)
    canonicalized = canonicalize_same_document_links(
        normalized_html,
        source_url=source_url,
        canonical_url=inferred_canonical_url,
    )
    article_html = absolutize_root_relative_urls(
        canonicalized.html,
        base_url=_root_relative_url_base(
            kind=kind,
            canonical_url=inferred_canonical_url,
            source_url=source_url,
        ),
    )
    if kind == WebHtmlKind.ARXIV_LATEXML:
        article_html = absolutize_arxiv_extracted_asset_urls(
            article_html,
            base_url=_root_relative_url_base(
                kind=kind,
                canonical_url=inferred_canonical_url,
                source_url=source_url,
            ),
        )
        article_html = normalize_latexml_equation_alignment(article_html)
        article_html = remove_latexml_table_color_artifacts(article_html)
        article_html = remove_latexml_description_error_panels(article_html)
        article_html = remove_latexml_inline_black_text_color(article_html)
    article_html = repair_empty_image_sources(article_html)
    article_html = repair_web_article_block_flow(article_html)
    article_html = remove_empty_tables(article_html)
    article_html = remove_empty_figure_shells(article_html)
    article_html = remove_unresolved_local_fragment_hrefs(article_html)
    wrapped = _wrap_web_article_html(
        article_html,
        kind=kind,
        title=title,
        article_selector=extraction.selector,
    )
    wrapped = render_katex_html(wrapped, ensure_head=_ensure_web_html_head)
    return WebHtmlPolishResult(
        html=wrapped,
        kind=kind,
        article_extracted=extraction.extracted,
        article_selector=extraction.selector,
        same_document_links_rewritten=canonicalized.rewritten_count,
        unresolved_same_document_links=canonicalized.unresolved_count,
    )


def polish_web_html_file(
    html_path: Path,
    *,
    source_url: str | None = None,
    canonical_url: str | None = None,
    fetch_text: "RemoteHtmlFetcher | None" = None,
) -> WebHtmlFilePolishResult:
    """Polish a web-native HTML file and inline local sidecar images."""

    html = html_path.read_text(encoding="utf-8", errors="replace")
    document = polish_web_html_document(
        html,
        source_url=source_url,
        canonical_url=canonical_url,
        fetch_text=fetch_text,
    )
    recovered_source_figures = 0
    html_for_inlining = document.html
    attempted_source_figures = 0
    source_recovery_errors: tuple[str, ...] = ()
    if document.kind == WebHtmlKind.ARXIV_LATEXML:
        recovery = recover_latexml_figures_from_arxiv_source_html(document.html, source_url=source_url)
        html_for_inlining = recovery.html
        recovered_source_figures = recovery.recovered_figures
        attempted_source_figures = recovery.attempted_figures
        source_recovery_errors = recovery.errors
    inlined = inline_local_images_from_web_html_document(html_for_inlining, base_dir=html_path.parent)
    remote_inlined = inline_remote_images_from_web_html_document(
        inlined.html,
        allowed_hosts=_remote_image_hosts_for_kind(document.kind),
    )
    normalized_html = _prefer_picture_inline_img_sources(
        _normalize_inline_data_image_mime_types(remote_inlined.html)
    )
    return WebHtmlFilePolishResult(
        html=normalized_html,
        kind=document.kind,
        article_extracted=document.article_extracted,
        article_selector=document.article_selector,
        same_document_links_rewritten=document.same_document_links_rewritten,
        unresolved_same_document_links=document.unresolved_same_document_links,
        inlined_images=inlined.inlined_images + remote_inlined.inlined_images,
        recovered_source_figures=recovered_source_figures,
        attempted_source_figures=attempted_source_figures,
        source_recovery_errors=source_recovery_errors,
    )


def unwrap_existing_web_polish_document(html: str) -> str:
    """Remove prior z2m readability wrappers before re-polishing source HTML."""

    cleaned = html
    while True:
        unwrapped = False
        for match in _HTML_TAG_RE.finditer(cleaned):
            if match.group(0).startswith("</") or match.group("tag").lower() != "main":
                continue
            attrs = match.group("attrs") or ""
            if (_attr_value(attrs, "id") or "") != "web-doc":
                continue
            fragment = _balanced_element_from_match(cleaned, match)
            if fragment is None:
                continue
            cleaned = fragment[len(match.group(0)) : -len("</main>")].strip()
            unwrapped = True
            break
        if not unwrapped:
            return cleaned


def unwrap_elements_by_attr_tokens(
    html: str,
    tokens: tuple[str, ...],
    *,
    tags: tuple[str, ...],
) -> str:
    """Remove matching wrapper tags while preserving their inner article content."""

    lowered_tokens = tuple(token.lower() for token in tokens)
    allowed_tags = {tag.lower() for tag in tags}
    cleaned = html
    previous = None
    while cleaned != previous:
        previous = cleaned
        for match in list(_HTML_TAG_RE.finditer(cleaned)):
            if match.group(0).startswith("</"):
                continue
            tag = match.group("tag").lower()
            if tag not in allowed_tags:
                continue
            attrs = unescape(match.group("attrs") or "").lower()
            if not any(token in attrs for token in lowered_tokens):
                continue
            fragment = _balanced_element_from_match(cleaned, match)
            if fragment is None:
                continue
            inner = fragment[len(match.group(0)) : -len(f"</{tag}>")]
            cleaned = cleaned[: match.start()] + inner + cleaned[match.start() + len(fragment) :]
            break
    return cleaned


def normalize_frontiers_reference_links(html: str) -> str:
    """Convert Frontiers JS citation buttons into durable local bibliography links."""

    if "ArticleReference" not in html:
        return html
    targets_by_lower = {target.lower(): target for target in extract_html_fragment_targets(html)}

    def replace_button(match: re.Match[str]) -> str:
        attrs = match.group("attrs") or ""
        class_name = _attr_value(attrs, "class") or ""
        if "ArticleReference" not in class_name.split():
            return match.group(0)
        reference_id = _frontiers_reference_id_from_attrs(attrs)
        target_id = targets_by_lower.get(reference_id.lower()) if reference_id else None
        if not target_id:
            return match.group(0)
        body = match.group("body")
        original_id = _attr_value(attrs, "id") or ""
        link_attrs = [
            'class="ArticleReference z2m-frontiers-citation"',
            f'href="#{html_escape(target_id, quote=True)}"',
        ]
        if original_id:
            link_attrs.append(f'id="{html_escape(original_id, quote=True)}"')
        return f"<a {' '.join(link_attrs)}>{body}</a>"

    def replace_anchor(match: re.Match[str]) -> str:
        attrs = match.group("attrs") or ""
        class_name = _attr_value(attrs, "class") or ""
        if "ArticleReference" not in class_name.split():
            return match.group(0)
        if (_attr_value(attrs, "href") or "").strip():
            return match.group(0)
        reference_id = _frontiers_reference_id_from_attrs(attrs)
        if not reference_id:
            return match.group(0)
        body = match.group("body")
        target_id = targets_by_lower.get(reference_id.lower())
        if target_id:
            link_attrs = [
                'class="ArticleReference z2m-frontiers-citation"',
                f'href="#{html_escape(target_id, quote=True)}"',
            ]
            return f"<a {' '.join(link_attrs)}>{body}</a>"
        escaped_reference_id = html_escape(reference_id, quote=True)
        return (
            '<span class="ArticleReference z2m-frontiers-unresolved-reference" '
            f'data-z2m-unresolved-reference="{escaped_reference_id}">{body}</span>'
        )

    html = _FRONTIERS_REFERENCE_BUTTON_RE.sub(replace_button, html)
    return _FRONTIERS_REFERENCE_ANCHOR_RE.sub(replace_anchor, html)


def _frontiers_reference_id_from_attrs(attrs: str) -> str | None:
    button_id = _attr_value(attrs, "id") or ""
    match = re.match(r"(?P<id>[A-Za-z]+\d+[A-Za-z]*)-button$", button_id, flags=re.IGNORECASE)
    if match is not None:
        return match.group("id")
    data_event = _attr_value(attrs, "data-event") or ""
    match = re.search(r"articleReference-a-(?P<id>[A-Za-z]+\d+[A-Za-z]*)\b", data_event, flags=re.IGNORECASE)
    if match is not None:
        return match.group("id")
    return None


def inline_local_images_from_web_html_document(html: str, *, base_dir: Path) -> InlineHtmlResult:
    """Inline local ``<img src>`` and ``srcset`` references without Marker polish."""

    base_dir = base_dir.resolve(strict=False)
    inlined_count = 0

    def inline_src_value(src_value: str) -> str | None:
        nonlocal inlined_count
        if not src_value or _is_nonlocal_image_src(src_value):
            return None

        candidate = _resolve_local_asset(src_value, base_dir=base_dir)
        if candidate is None:
            return None

        data_url = to_data_url(candidate, detect_by_signature=True, log_func=None)
        if data_url is None or not validate_data_url(data_url, candidate):
            return None

        inlined_count += 1
        return data_url

    def replace_src(match: re.Match[str]) -> str:
        nonlocal inlined_count
        prefix = match.group("prefix")
        quote = match.group("quote")
        src_value = unescape(match.group("src")).strip()
        data_url = inline_src_value(src_value)
        if data_url is None:
            return match.group(0)

        prefix = _add_src_hint(prefix, src_value)
        return f"{prefix}{quote}{data_url}{quote}"

    def replace_srcset(match: re.Match[str]) -> str:
        prefix = match.group("prefix")
        quote = match.group("quote")
        srcset = unescape(match.group("srcset")).strip()
        if "data:" in srcset.lower():
            return match.group(0)
        next_entries: list[str] = []
        changed = False
        for raw_entry in srcset.split(","):
            entry = raw_entry.strip()
            if not entry:
                continue
            parts = entry.split()
            src_value = parts[0]
            descriptor = " ".join(parts[1:])
            data_url = inline_src_value(src_value)
            if data_url is None:
                next_entries.append(entry)
                continue
            changed = True
            next_entries.append(f"{data_url} {descriptor}".strip())
        if not changed:
            return match.group(0)
        escaped_srcset = html_escape(", ".join(next_entries), quote=True)
        return f"{prefix}{quote}{escaped_srcset}{quote}"

    html = _IMG_SRC_RE.sub(replace_src, html)
    html = _SRCSET_RE.sub(replace_srcset, html)
    return InlineHtmlResult(html=html, inlined_images=inlined_count)


def _normalize_inline_data_image_mime_types(html: str) -> str:
    """Repair embedded image data URLs whose MIME type is too generic for browsers."""

    def replace(match: re.Match[str]) -> str:
        rewritten = _normalize_inline_data_image_src(unescape(match.group("src")).strip())
        if rewritten is None:
            return match.group(0)
        quote = match.group("quote")
        return f"{match.group('prefix')}{quote}{html_escape(rewritten, quote=True)}{quote}"

    return _IMG_SRC_RE.sub(replace, html)


def _prefer_picture_inline_img_sources(html: str) -> str:
    """Prevent remote ``source srcset`` entries from overriding embedded ``img`` fallbacks."""

    def replace(match: re.Match[str]) -> str:
        body = match.group("body")
        has_inline_img = False
        for img_match in _IMG_OPEN_RE.finditer(body):
            src = (_attr_value(img_match.group("attrs") or "", "src") or "").strip().lower()
            if src.startswith("data:image/"):
                has_inline_img = True
                break
        if not has_inline_img:
            return match.group(0)
        body = _SOURCE_OPEN_RE.sub("", body)
        return f"<picture{match.group('attrs')}>{body}</picture>"

    return _PICTURE_RE.sub(replace, html)


def _normalize_inline_data_image_src(src_value: str) -> str | None:
    if not src_value.lower().startswith("data:"):
        return None
    comma_idx = src_value.find(",")
    if comma_idx < 0:
        return None
    meta = src_value[5:comma_idx]
    payload = src_value[comma_idx + 1 :]
    meta_parts = [part.strip() for part in meta.split(";") if part.strip()]
    declared_mime = meta_parts[0].lower() if meta_parts and "/" in meta_parts[0] else ""
    params = meta_parts[1:] if declared_mime else meta_parts
    if declared_mime.startswith("image/"):
        return None
    if not any(part.lower() == "base64" for part in params):
        return None
    blob_prefix = _decode_base64_prefix(payload)
    if not blob_prefix:
        return None
    mime = _image_mime_from_blob_prefix(blob_prefix)
    if mime is None:
        return None
    suffix = ";".join(params) or "base64"
    return f"data:{mime};{suffix},{payload}"


def _decode_base64_prefix(payload: str) -> bytes:
    sample = re.sub(r"\s+", "", payload[:512])[:128]
    if not sample:
        return b""
    sample += "=" * ((4 - len(sample) % 4) % 4)
    try:
        return base64.b64decode(sample)
    except Exception:
        return b""


def _image_mime_from_blob_prefix(blob: bytes) -> str | None:
    for signature, mime in sorted(IMAGE_SIGNATURES.items(), key=lambda item: len(item[0]), reverse=True):
        if not mime.startswith("image/"):
            continue
        if not blob.startswith(signature):
            continue
        if signature == b"RIFF" and blob[8:12] != b"WEBP":
            continue
        return mime
    return None


RemoteHtmlFetcher = Callable[[str], str]
RemoteImageFetcher = Callable[[str], tuple[bytes, str | None]]


def inline_remote_images_from_web_html_document(
    html: str,
    *,
    allowed_hosts: frozenset[str] = frozenset(),
    fetch_bytes: RemoteImageFetcher | None = None,
) -> InlineHtmlResult:
    """Inline allowed remote image URLs for Zotero-friendly source HTML."""

    if not allowed_hosts:
        return InlineHtmlResult(html=html, inlined_images=0)

    fetcher = fetch_bytes or _fetch_remote_image
    cache: dict[str, str | None] = {}
    inlined_count = 0

    def inline_src_value(src_value: str) -> str | None:
        nonlocal inlined_count
        url = unescape(src_value).strip()
        if not _is_allowed_remote_image_url(url, allowed_hosts=allowed_hosts):
            return None
        if url not in cache:
            try:
                blob, content_type = fetcher(url)
                cache[url] = _remote_image_data_url(url, blob, content_type)
            except Exception:
                cache[url] = None
        data_url = cache[url]
        if data_url is None:
            return None
        inlined_count += 1
        return data_url

    def replace_src(match: re.Match[str]) -> str:
        prefix = match.group("prefix")
        quote = match.group("quote")
        src_value = unescape(match.group("src")).strip()
        data_url = inline_src_value(src_value)
        if data_url is None:
            return match.group(0)
        prefix = _add_src_hint(prefix, src_value)
        return f"{prefix}{quote}{data_url}{quote}"

    def replace_srcset(match: re.Match[str]) -> str:
        prefix = match.group("prefix")
        quote = match.group("quote")
        srcset = unescape(match.group("srcset")).strip()
        if "data:" in srcset.lower():
            return match.group(0)
        next_entries: list[str] = []
        changed = False
        for raw_entry in srcset.split(","):
            entry = raw_entry.strip()
            if not entry:
                continue
            parts = entry.split()
            src_value = parts[0]
            descriptor = " ".join(parts[1:])
            data_url = inline_src_value(src_value)
            if data_url is None:
                next_entries.append(entry)
                continue
            changed = True
            next_entries.append(f"{data_url} {descriptor}".strip())
        if not changed:
            return match.group(0)
        escaped_srcset = html_escape(", ".join(next_entries), quote=True)
        return f"{prefix}{quote}{escaped_srcset}{quote}"

    html = _IMG_SRC_RE.sub(replace_src, html)
    html = _SRCSET_RE.sub(replace_srcset, html)
    return InlineHtmlResult(html=html, inlined_images=inlined_count)


def _remote_image_hosts_for_kind(kind: WebHtmlKind) -> frozenset[str]:
    if kind == WebHtmlKind.IOP_ARTICLE:
        return frozenset({"content.cld.iop.org"})
    if kind == WebHtmlKind.ARXIV_LATEXML:
        return frozenset({"arxiv.org", "www.arxiv.org"})
    return frozenset()


def _is_allowed_remote_image_url(url: str, *, allowed_hosts: frozenset[str]) -> bool:
    parsed = _urlsplit_or_none(url)
    if parsed is None or parsed.scheme.lower() not in {"http", "https"}:
        return False
    return parsed.netloc.lower().split(":", 1)[0] in allowed_hosts


def _fetch_remote_image(url: str) -> tuple[bytes, str | None]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 z2m-web-polish"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        content_type = response.headers.get("Content-Type")
        return response.read(), content_type


def _fetch_remote_html(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "User-Agent": "Mozilla/5.0 z2m-web-polish",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        content_type = response.headers.get("Content-Type") or ""
        charset = response.headers.get_content_charset() or "utf-8"
        body = response.read()
    if "text/html" not in content_type.lower() and b"<html" not in body[:4096].lower():
        raise WebHtmlPolishError(f"Fetched Sciendo full-text URL is not HTML: {url}")
    return body.decode(charset, errors="replace")


def _sciendo_full_text_url(html: str, *, source_url: str | None) -> str | None:
    for name in ("citation_full_html_url", "dc.identifier.uri"):
        for candidate in _meta_contents(html, name):
            resolved = _valid_sciendo_article_url(candidate)
            if resolved is not None:
                return resolved
    if source_url:
        parsed = _urlsplit_or_none(source_url)
        if parsed is not None and _is_sciendo_or_reference_global_host(parsed.netloc):
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            if "tab" in query and query.get("tab") != ["article"]:
                query["tab"] = ["article"]
                return urllib.parse.urlunsplit(
                    (
                        parsed.scheme,
                        parsed.netloc,
                        parsed.path,
                        urllib.parse.urlencode(query, doseq=True),
                        parsed.fragment,
                    )
                )
    return None


def _meta_contents(html: str, name: str) -> list[str]:
    expected = name.lower()
    values: list[str] = []
    for match in _META_TAG_RE.finditer(html):
        attrs = match.group("attrs")
        meta_name = (_attr_value(attrs, "name") or _attr_value(attrs, "property") or "").lower()
        if meta_name != expected:
            continue
        content = (_attr_value(attrs, "content") or "").strip()
        if content:
            values.append(unescape(content))
    return values


def _valid_sciendo_article_url(url: str) -> str | None:
    parsed = _urlsplit_or_none(url.strip())
    if parsed is None or parsed.scheme.lower() not in {"http", "https"}:
        return None
    if not _is_sciendo_or_reference_global_host(parsed.netloc):
        return None
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    if query.get("tab") == ["article"]:
        return url.strip()
    if "/article/" not in parsed.path:
        return None
    query["tab"] = ["article"]
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(query, doseq=True),
            parsed.fragment,
        )
    )


def _is_sciendo_or_reference_global_host(netloc: str) -> bool:
    host = netloc.lower().split(":", 1)[0]
    return host.endswith("sciendo.com") or host.endswith("reference-global.com")


def _remote_image_data_url(url: str, blob: bytes, content_type: str | None) -> str | None:
    mime = _remote_image_mime(url, blob, content_type)
    if mime is None:
        return None
    encoded = base64.b64encode(blob).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _remote_image_mime(url: str, blob: bytes, content_type: str | None) -> str | None:
    header_mime = (content_type or "").split(";", 1)[0].strip().lower()
    if header_mime.startswith("image/"):
        return header_mime
    for signature, mime in IMAGE_SIGNATURES.items():
        if blob.startswith(signature) and mime.startswith("image/"):
            return mime
    parsed = _urlsplit_or_none(url)
    path = parsed.path if parsed is not None else url
    guessed, _ = mimetypes.guess_type(path)
    if guessed and guessed.startswith("image/"):
        return guessed
    return None


def absolutize_root_relative_urls(html: str, *, base_url: str | None) -> str:
    """Rewrite ``/...`` publisher links so local ``file://`` viewing does not hijack them."""

    parsed_base = _urlsplit_or_none(base_url)
    if parsed_base is None or not parsed_base.scheme or not parsed_base.netloc:
        return html

    origin = urllib.parse.urlunsplit((parsed_base.scheme, parsed_base.netloc, "/", "", ""))

    def absolute_url(raw_url: str) -> str | None:
        value = unescape(raw_url).strip()
        if not _is_root_relative_url(value):
            return None
        return urllib.parse.urljoin(origin, value)

    def replace_attr(match: re.Match[str]) -> str:
        rewritten = absolute_url(match.group("url"))
        if rewritten is None:
            return match.group(0)
        quote = match.group("quote")
        return f"{match.group('prefix')}{quote}{html_escape(rewritten, quote=True)}{quote}"

    def replace_srcset(match: re.Match[str]) -> str:
        changed = False
        entries: list[str] = []
        for raw_entry in match.group("srcset").split(","):
            entry = raw_entry.strip()
            if not entry:
                continue
            parts = entry.split()
            rewritten = absolute_url(parts[0])
            if rewritten is None:
                entries.append(entry)
                continue
            changed = True
            descriptor = " ".join(parts[1:])
            entries.append(f"{rewritten} {descriptor}".strip())
        if not changed:
            return match.group(0)
        quote = match.group("quote")
        return f"{match.group('prefix')}{quote}{html_escape(', '.join(entries), quote=True)}{quote}"

    html = _ROOT_RELATIVE_URL_ATTR_RE.sub(replace_attr, html)
    html = _SRCSET_RE.sub(replace_srcset, html)
    return html


def absolutize_arxiv_extracted_asset_urls(html: str, *, base_url: str | None) -> str:
    """Rewrite arXiv LaTeXML ``extracted/...`` assets to fetchable HTML URLs."""

    parsed_base = _urlsplit_or_none(base_url)
    if parsed_base is None or parsed_base.netloc.lower() not in {"arxiv.org", "www.arxiv.org"}:
        return html
    if _arxiv_html_parts(parsed_base) is None:
        return html
    document_base = urllib.parse.urlunsplit(
        (parsed_base.scheme, parsed_base.netloc, parsed_base.path.rstrip("/") + "/", "", "")
    )

    def absolute_url(raw_url: str) -> str | None:
        value = unescape(raw_url).strip()
        if not value.lower().startswith("extracted/"):
            return None
        return urllib.parse.urljoin(document_base, value)

    def replace_src(match: re.Match[str]) -> str:
        rewritten = absolute_url(match.group("src"))
        if rewritten is None:
            return match.group(0)
        quote = match.group("quote")
        return f"{match.group('prefix')}{quote}{html_escape(rewritten, quote=True)}{quote}"

    def replace_srcset(match: re.Match[str]) -> str:
        changed = False
        entries: list[str] = []
        for raw_entry in match.group("srcset").split(","):
            entry = raw_entry.strip()
            if not entry:
                continue
            parts = entry.split()
            rewritten = absolute_url(parts[0])
            if rewritten is None:
                entries.append(entry)
                continue
            changed = True
            descriptor = " ".join(parts[1:])
            entries.append(f"{rewritten} {descriptor}".strip())
        if not changed:
            return match.group(0)
        quote = match.group("quote")
        return f"{match.group('prefix')}{quote}{html_escape(', '.join(entries), quote=True)}{quote}"

    html = _IMG_SRC_RE.sub(replace_src, html)
    html = _SRCSET_RE.sub(replace_srcset, html)
    return html


def _root_relative_url_base(
    *,
    kind: WebHtmlKind,
    canonical_url: str | None,
    source_url: str | None,
) -> str | None:
    for raw_url in (canonical_url, source_url):
        parsed = _urlsplit_or_none(raw_url)
        if parsed is not None and parsed.scheme and parsed.netloc and not _is_doi_host(parsed.netloc):
            return raw_url
    return _publisher_default_origin(kind)


def _publisher_default_origin(kind: WebHtmlKind) -> str | None:
    return default_origin_for_kind(kind)


def _is_doi_host(host: str) -> bool:
    normalized = host.lower().split(":", 1)[0]
    return normalized in {"doi.org", "dx.doi.org", "www.doi.org"}


def extract_web_article_fragment(html: str, *, kind: WebHtmlKind | None = None) -> WebArticleExtraction:
    """Extract the central article-like fragment from third-party web HTML."""

    source_specific = _extract_source_specific_article_fragment(html, kind=kind)
    if source_specific is not None:
        return source_specific
    return extract_generic_web_article_fragment(html, kind=kind)


def normalize_web_article_fragment(
    html: str,
    *,
    kind: WebHtmlKind,
    source_url: str | None = None,
    canonical_url: str | None = None,
    fetch_text: "RemoteHtmlFetcher | None" = None,
) -> str:
    """Apply publisher-specific static normalizations after extraction."""

    return normalize_source_specific_article_fragment(
        html,
        kind=kind,
        source_url=source_url,
        canonical_url=canonical_url,
        fetch_text=fetch_text,
    )


def remove_publisher_ui_fragments(html: str) -> str:
    """Drop article-page chrome that often survives generic extraction."""

    html = _remove_elements_by_attr_tokens(
        html,
        (
            "citation-dialog-trigger",
            "collections-dialog-trigger",
            "collections-dialog",
            "collections-action-dialog-form",
            "collections-action-panel-form",
            "collections-action-panel",
            "export-button",
            "pmc-permalink",
            "pmc-permalink__dropdown__copy__btn",
            "usa-accordion__button",
            "journal_context_menu",
            "show article permalink",
            "d-button",
            "d-buttons",
        ),
        tags=("a", "button", "form", "div", "li", "nav", "section", "aside", "ul"),
    )
    html = _remove_elements_by_attr_tokens(
        html,
        (
            "ButtonIcon",
            "articleFigure-button-download",
            "articleFigure-button-openLightbox",
            "articleTable-button-openLightbox",
        ),
        tags=("button",),
    )
    html = unwrap_elements_by_attr_tokens(
        html,
        ("ArticleFigure__figureButton",),
        tags=("button",),
    )
    html = _remove_elements_by_attr_tokens(
        html,
        (
            "ArticleMetrics",
            "articleMetrics",
            "article-metrics",
            "article_metrics",
            "data-event=\"articleMetrics",
            "data-event='articleMetrics",
            "altmetric",
        ),
        tags=("aside", "div", "section", "nav"),
    )
    return html


def remove_unresolved_local_fragment_hrefs(html: str) -> str:
    """Keep link text but disable local fragment links whose target is absent."""

    targets = extract_html_fragment_targets(html)

    def replace(match: re.Match[str]) -> str:
        href = unescape(match.group("href")).strip()
        parsed = _urlsplit_or_none(href)
        if parsed is None or not is_plain_local_fragment(parsed):
            return match.group(0)
        target = urllib.parse.unquote(parsed.fragment)
        if target in targets:
            return match.group(0)
        quote = match.group("quote")
        escaped_href = html_escape(href, quote=True)
        return f"data-z2m-unresolved-href={quote}{escaped_href}{quote}"

    return _ATTR_HREF_RE.sub(replace, html)


def repair_empty_image_sources(html: str) -> str:
    """Restore lazy image sources or remove broken empty ``img`` placeholders."""

    def replace(match: re.Match[str]) -> str:
        open_tag = match.group(0)
        attrs = match.group("attrs") or ""
        src = (_attr_value(attrs, "src") or "").strip()
        if src:
            return open_tag
        srcset = (_attr_value(attrs, "srcset") or "").strip()
        if srcset:
            return open_tag
        for attr_name in ("data-src", "data-original", "data-lazy-src"):
            candidate = (_attr_value(attrs, attr_name) or "").strip()
            if candidate:
                return _set_attr_value(open_tag, "src", candidate)
        return ""

    return _IMG_OPEN_RE.sub(replace, html)


def repair_web_article_block_flow(html: str) -> str:
    """Repair common PDF-like block nesting that makes browser text flow unstable."""

    html = lift_block_children_from_paragraphs(html)
    return wrap_standalone_image_paragraphs(html)


def lift_block_children_from_paragraphs(html: str) -> str:
    """Move lists, tables, and figures out of paragraph wrappers."""

    cleaned = html
    while True:
        for match in _HTML_TAG_RE.finditer(cleaned):
            if match.group(0).startswith("</") or match.group("tag").lower() != "p":
                continue
            fragment = _balanced_element_from_match(cleaned, match)
            if fragment is None:
                continue
            attrs = match.group("attrs") or ""
            inner = fragment[len(match.group(0)) : -len("</p>")]
            repaired = _lift_paragraph_block_children(attrs, inner)
            if repaired == fragment:
                continue
            cleaned = cleaned[: match.start()] + repaired + cleaned[match.start() + len(fragment) :]
            break
        else:
            return cleaned


def wrap_standalone_image_paragraphs(html: str) -> str:
    """Convert image-only paragraphs into figures so they do not split text inline."""

    cleaned = html
    while True:
        for match in _HTML_TAG_RE.finditer(cleaned):
            if match.group(0).startswith("</") or match.group("tag").lower() != "p":
                continue
            fragment = _balanced_element_from_match(cleaned, match)
            if fragment is None:
                continue
            inner = fragment[len(match.group(0)) : -len("</p>")].strip()
            if not _is_standalone_image_fragment(inner):
                continue
            attrs = _figure_attrs_from_paragraph_attrs(match.group("attrs") or "")
            replacement = f"<figure{attrs}>{inner}</figure>"
            cleaned = cleaned[: match.start()] + replacement + cleaned[match.start() + len(fragment) :]
            break
        else:
            return cleaned


def _lift_paragraph_block_children(attrs: str, inner: str) -> str:
    pieces: list[str] = []
    cursor = 0
    changed = False
    for match in _HTML_TAG_RE.finditer(inner):
        if match.start() < cursor or match.group(0).startswith("</"):
            continue
        tag = match.group("tag").lower()
        if tag not in _PARAGRAPH_BLOCK_TAGS:
            continue
        fragment = _balanced_element_from_match(inner, match)
        if fragment is None:
            continue
        before = inner[cursor : match.start()]
        if _has_paragraph_flow_content(before):
            pieces.append(f"<p{attrs}>{before.strip()}</p>")
        pieces.append(fragment.strip())
        cursor = match.start() + len(fragment)
        changed = True
    if not changed:
        return f"<p{attrs}>{inner}</p>"
    tail = inner[cursor:]
    if _has_paragraph_flow_content(tail):
        pieces.append(f"<p{attrs}>{tail.strip()}</p>")
    return "\n".join(pieces)


def _has_paragraph_flow_content(fragment: str) -> bool:
    if not fragment.strip():
        return False
    return bool(_visible_text(fragment).strip() or _INLINE_MEDIA_RE.search(fragment))


def _is_standalone_image_fragment(fragment: str) -> bool:
    if _IMG_OPEN_RE.search(fragment) is None:
        return False
    without_images = _IMG_OPEN_RE.sub("", fragment)
    without_breaks = re.sub(r"<br\b[^>]*>", "", without_images, flags=re.IGNORECASE)
    return not _has_paragraph_flow_content(without_breaks)


def _figure_attrs_from_paragraph_attrs(attrs: str) -> str:
    attr_values: list[str] = []
    id_value = (_attr_value(attrs, "id") or "").strip()
    if id_value:
        attr_values.append(f'id="{html_escape(id_value, quote=True)}"')
    class_value = (_attr_value(attrs, "class") or "").strip()
    classes = "z2m-standalone-media"
    if class_value:
        classes = f"{classes} {class_value}"
    attr_values.append(f'class="{html_escape(classes, quote=True)}"')
    return " " + " ".join(attr_values)


def remove_empty_tables(html: str) -> str:
    previous = None
    cleaned = html
    while cleaned != previous:
        previous = cleaned
        cleaned = _EMPTY_TABLE_RE.sub("", cleaned)
    return cleaned


def remove_empty_figure_shells(html: str) -> str:
    previous = None
    cleaned = html
    while cleaned != previous:
        previous = cleaned
        cleaned = _EMPTY_FIGURE_SHELL_RE.sub("", cleaned)
    return cleaned


def remove_latexml_table_color_artifacts(html: str) -> str:
    html = _LTX_ROWCOLOR_ARTIFACT_RE.sub("", html)
    return _LTX_ROWCOLOR_TEXT_RE.sub("", html)


def remove_latexml_inline_black_text_color(html: str) -> str:
    """Let the readability theme control LaTeXML text/math color instead of hard-coded black."""

    def replace(match: re.Match[str]) -> str:
        tag = match.group(0)
        if tag.startswith("</"):
            return tag
        attrs = match.group("attrs") or ""
        cleaned_tag = tag
        if match.group("tag").lower() == "span" and "ltx_text" in (
            _attr_value(attrs, "class") or ""
        ).split():
            style = _attr_value(attrs, "style") or ""
            cleaned_style = _strip_black_foreground_declaration(style)
            if cleaned_style != style:
                cleaned_tag = (
                    _set_attr_value(cleaned_tag, "style", cleaned_style)
                    if cleaned_style
                    else _remove_attr_value(cleaned_tag, "style")
                )
        mathcolor = (_attr_value(attrs, "mathcolor") or "").strip().casefold()
        if mathcolor in {"#000", "#000000", "black"}:
            cleaned_tag = _remove_attr_value(cleaned_tag, "mathcolor")
        return cleaned_tag

    return _HTML_TAG_RE.sub(replace, html)


def _strip_black_foreground_declaration(style: str) -> str:
    declarations: list[str] = []
    changed = False
    for match in _CSS_DECLARATION_RE.finditer(style):
        name = match.group("name").strip().casefold()
        value = match.group("value").strip().casefold()
        if name == "color" and value in {"#000", "#000000", "black"}:
            changed = True
            continue
        declarations.append(f"{match.group('name').strip()}:{match.group('value').strip()}")
    if not changed:
        return style
    return ";".join(declarations) + (";" if declarations else "")


def _remove_attr_value(tag: str, name: str) -> str:
    return re.sub(
        _ATTR_RE_TEMPLATE.format(name=re.escape(name)),
        "",
        tag,
        count=1,
        flags=re.IGNORECASE | re.DOTALL,
    )


def remove_latexml_description_error_panels(html: str) -> str:
    """Drop LaTeXML ``\\Description`` error panels without losing real media/table content."""

    cleaned = html
    while True:
        for match in _HTML_TAG_RE.finditer(cleaned):
            if match.group(0).startswith("</") or match.group("tag").lower() != "div":
                continue
            attrs = match.group("attrs") or ""
            if "ltx_flex_figure" not in (_attr_value(attrs, "class") or "").split():
                continue
            fragment = _balanced_element_from_match(cleaned, match)
            if fragment is None or not _looks_like_latexml_description_panel(fragment):
                continue
            replacement = _latexml_description_panel_replacement(fragment, match.group(0))
            cleaned = cleaned[: match.start()] + replacement + cleaned[match.start() + len(fragment) :]
            break
        else:
            return cleaned


def _looks_like_latexml_description_panel(fragment: str) -> bool:
    return "ltx_ERROR" in fragment and _LTX_DESCRIPTION_TEXT_RE.search(fragment) is not None


def _latexml_description_panel_replacement(fragment: str, open_tag: str) -> str:
    inner = fragment[len(open_tag) : -len("</div>")]
    inner = _rewrite_latexml_description_panel_children(inner)
    if not _has_structural_latexml_content(inner):
        return ""
    return inner.strip()


def _rewrite_latexml_description_panel_children(html: str) -> str:
    cleaned = html
    while True:
        for match in _HTML_TAG_RE.finditer(cleaned):
            if match.group(0).startswith("</") or match.group("tag").lower() != "div":
                continue
            attrs = match.group("attrs") or ""
            class_tokens = set((_attr_value(attrs, "class") or "").split())
            if not class_tokens & {"ltx_flex_cell", "ltx_flex_break"}:
                continue
            fragment = _balanced_element_from_match(cleaned, match)
            if fragment is None:
                continue
            if "ltx_flex_break" in class_tokens or _looks_like_latexml_description_panel(fragment):
                replacement = ""
            else:
                replacement = fragment[len(match.group(0)) : -len("</div>")].strip()
            cleaned = cleaned[: match.start()] + replacement + cleaned[match.start() + len(fragment) :]
            break
        else:
            return cleaned


def _has_structural_latexml_content(html: str) -> bool:
    sample = html.casefold()
    return any(
        marker in sample
        for marker in (
            "<img",
            "<math",
            "<object",
            "<picture",
            "<svg",
            "<table",
            "<video",
            "ltx_graphics",
            "ltx_tabular",
        )
    )


def normalize_latexml_equation_alignment(html: str) -> str:
    """Center single-cell LaTeXML equation rows while preserving aligned systems."""

    def replace_row(match: re.Match[str]) -> str:
        row_attrs = match.group("attrs") or ""
        row_class = _attr_value(row_attrs, "class") or ""
        if "ltx_equation" not in row_class.split():
            return match.group(0)

        body = match.group("body") or ""
        content_cells: list[re.Match[str]] = []
        for cell_match in _TD_OPEN_RE.finditer(body):
            cell_class = _attr_value(cell_match.group("attrs") or "", "class") or ""
            class_tokens = set(cell_class.split())
            if class_tokens & {
                "ltx_eqn_center_padleft",
                "ltx_eqn_center_padright",
                "ltx_eqn_eqno",
            }:
                continue
            content_cells.append(cell_match)

        if len(content_cells) != 1:
            return match.group(0)

        cell_match = content_cells[0]
        open_tag = cell_match.group(0)
        cell_class = _attr_value(cell_match.group("attrs") or "", "class") or ""
        if "z2m-ltx-single-equation-cell" in cell_class.split():
            return match.group(0)
        updated_open_tag = _set_attr_value(
            open_tag,
            "class",
            f"{cell_class} z2m-ltx-single-equation-cell".strip(),
        )
        updated_body = body[: cell_match.start()] + updated_open_tag + body[cell_match.end() :]
        return f"<tr{row_attrs}>{updated_body}</tr>"

    return _LTX_EQUATION_ROW_RE.sub(replace_row, html)


def _extract_source_specific_article_fragment(
    html: str,
    *,
    kind: WebHtmlKind | None,
) -> WebArticleExtraction | None:
    return extract_source_specific_article_fragment(html, kind=kind)


def _looks_like_arxiv_abs_page(sample: str) -> bool:
    return (
        'name="citation_arxiv_id"' in sample
        and ("html (experimental)" in sample or "latexml-download-link" in sample)
        and ("abs-button" in sample or "extra-services" in sample)
    )


def _looks_like_arxiv_latexml(
    sample: str,
    parsed_source: urllib.parse.SplitResult | None,
) -> bool:
    if "ltx_page_main" not in sample:
        return False
    if "generated" in sample and "latexml" in sample:
        return True
    if "ltx_bibliography" in sample or "ltx_title_document" in sample:
        return True
    return parsed_source is not None and _arxiv_html_parts(parsed_source) is not None


def _looks_like_pmc_article(sample: str, parsed_source: urllib.parse.SplitResult | None) -> bool:
    host = (parsed_source.netloc.lower() if parsed_source is not None else "")
    if host in {"pmc.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov"}:
        return True
    return "pmc-article" in sample or 'id="main-content"' in sample and "pmc" in sample


def _looks_like_taylor_francis_article(sample: str, parsed_source: urllib.parse.SplitResult | None) -> bool:
    host = (parsed_source.netloc.lower() if parsed_source is not None else "")
    if host.endswith("tandfonline.com"):
        return True
    return "hlfld-fulltext" in sample or "nlm_article" in sample


def _looks_like_springer_nature_article(sample: str, parsed_source: urllib.parse.SplitResult | None) -> bool:
    host = (parsed_source.netloc.lower() if parsed_source is not None else "")
    if host in {"link.springer.com", "www.nature.com"}:
        return True
    return "c-article-body" in sample or "article__body" in sample


def _looks_like_iop_article(sample: str, parsed_source: urllib.parse.SplitResult | None) -> bool:
    host = (parsed_source.netloc.lower() if parsed_source is not None else "")
    path = (parsed_source.path.lower() if parsed_source is not None else "")
    has_full_text_markers = _has_iop_full_text_markers(sample)
    if host.endswith("iopscience.iop.org") and path.startswith("/article/"):
        if path.endswith(("/meta", "/pdf", "/xml")):
            return has_full_text_markers
        return True
    if "citation_publisher\" content=\"iop publishing" in sample or "citation_publisher' content='iop publishing" in sample:
        return has_full_text_markers
    return "iopscience.iop.org/article/" in sample and has_full_text_markers


def _has_iop_full_text_markers(sample: str) -> bool:
    return (
        "wd-jnl-art-full-text" in sample
        or "itemprop=\"articlebody\"" in sample
        or "itemprop='articlebody'" in sample
        or "class=\"article-content\"" in sample
        or "class='article-content'" in sample
    )


def _looks_like_researchgate_page(sample: str, parsed_source: urllib.parse.SplitResult | None) -> bool:
    host = (parsed_source.netloc.lower() if parsed_source is not None else "")
    if host.endswith("researchgate.net"):
        return True
    return (
        "www.researchgate.net" in sample
        or "lite.publicationdetails" in sample
        or "research-detail-header-section" in sample
        or ("download full-text pdf" in sample and "researchgate" in sample)
    )


def _looks_like_researchgate_pdf_shell(html: str) -> bool:
    sample = html[:500_000].lower()
    if "<article" in sample or "article-content" in sample or "article__content" in sample:
        return False
    if _visible_text_length(html) >= 3_000:
        return False
    return (
        "download full-text pdf" in sample
        or "request full-text" in sample
        or "research-detail-header-section" in sample
        or "lite.publicationdetails" in sample
    )


def _looks_like_sciendo_abstract_page(
    sample: str,
    parsed_source: urllib.parse.SplitResult | None,
) -> bool:
    host = (parsed_source.netloc.lower() if parsed_source is not None else "")
    if host not in {"content.sciendo.com", "reference-global.com", "www.reference-global.com"} and not (
        "content.sciendo.com" in sample or "reference-global.com" in sample
    ):
        return False
    return (
        "content-tabs" in sample
        and "tab-button-article" in sample
        and "article-content" not in sample
        and ("abstract-content" in sample or "self.__next_f.push" in sample)
    )


def _looks_like_sciendo_article_page(
    sample: str,
    parsed_source: urllib.parse.SplitResult | None,
) -> bool:
    host = (parsed_source.netloc.lower() if parsed_source is not None else "")
    if host not in {"content.sciendo.com", "reference-global.com", "www.reference-global.com"} and not (
        "content.sciendo.com" in sample or "reference-global.com" in sample
    ):
        return False
    return "article-content" in sample and ("full article" in sample or "tab-button-article" in sample)


def _looks_like_ojs_abstract_page(
    sample: str,
    parsed_source: urllib.parse.SplitResult | None,
) -> bool:
    host = (parsed_source.netloc.lower() if parsed_source is not None else "")
    if not (
        host.endswith("almclinmed.ru")
        or "almclinmed.ru" in sample
        or "open journal systems" in sample
        or "pkp" in sample
    ):
        return False
    if 'id="articlefulltext"' not in sample or 'id="articleabstract"' not in sample:
        return False
    return "citation_pdf_url" in sample or "/article/download/" in sample or "class=\"file\"" in sample


def _document_title(html: str) -> str:
    for match in _META_TAG_RE.finditer(html[:500_000]):
        attrs = match.group("attrs") or ""
        name = (_attr_value(attrs, "name") or _attr_value(attrs, "property") or "").strip().lower()
        if name not in {"citation_title", "dc.title", "og:title"}:
            continue
        content = (_attr_value(attrs, "content") or "").strip()
        if content:
            return _visible_text(content) or content

    match = _TITLE_RE.search(html)
    if match is None:
        return "Web Article"
    title = _visible_text(match.group("title"))
    return title or "Web Article"


def _wrap_web_article_html(
    article_html: str,
    *,
    kind: WebHtmlKind,
    title: str,
    article_selector: str | None,
) -> str:
    escaped_title = html_escape(title, quote=False)
    escaped_kind = html_escape(kind.value, quote=True)
    selector_attr = ""
    if article_selector:
        selector_attr = f' data-z2m-article-selector="{html_escape(article_selector, quote=True)}"'
    heading = ""
    if title != "Web Article" and _H1_RE.search(article_html) is None:
        heading = f'<h1 class="z2m-web-title">{escaped_title}</h1>\n'
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"<title>{escaped_title}</title>\n"
        f"{web_readability_style()}\n"
        "</head>\n"
        "<body>\n"
        f'<main id="web-doc" data-z2m-source-kind="{escaped_kind}"{selector_attr}>\n'
        f"{heading}"
        f"{article_html}\n"
        "</main>\n"
        "</body>\n"
        "</html>\n"
    )


def _ensure_web_html_head(html: str) -> str:
    if re.search(r"<head\b", html, flags=re.IGNORECASE):
        return html
    if re.search(r"<html\b[^>]*>", html, flags=re.IGNORECASE):
        return re.sub(
            r"(<html\b[^>]*>)",
            r"\1\n<head><meta charset=\"utf-8\"></head>",
            html,
            count=1,
            flags=re.IGNORECASE,
        )
    return f'<!doctype html>\n<html lang="en">\n<head><meta charset="utf-8"></head>\n<body>{html}</body>\n</html>'


def _is_nonlocal_image_src(src_value: str) -> bool:
    lowered = src_value.lower()
    return lowered.startswith("file:") or is_inline_or_remote(src_value)


def _resolve_local_asset(src_value: str, *, base_dir: Path) -> Path | None:
    clean_src = src_value.split("?", 1)[0].split("#", 1)[0]
    if len(clean_src) > _MAX_LOCAL_ASSET_REFERENCE_LENGTH:
        return None
    decoded = urllib.parse.unquote(clean_src)
    if not decoded:
        return None
    raw_path = Path(decoded)
    if raw_path.is_absolute():
        return None
    candidate = (base_dir / raw_path).resolve(strict=False)
    try:
        candidate.relative_to(base_dir)
    except ValueError:
        return None
    try:
        is_file = candidate.is_file()
    except OSError:
        return None
    if not is_file:
        return None
    return candidate


def _add_src_hint(prefix: str, hint_path: str) -> str:
    if re.search(r"\bdata-z2m-src\s*=", prefix, re.IGNORECASE):
        return prefix
    escaped_hint = html_escape(hint_path, quote=True)
    return re.sub(
        r"\bsrc\s*=\s*$",
        f'data-z2m-src="{escaped_hint}" src=',
        prefix,
        flags=re.IGNORECASE,
    )
