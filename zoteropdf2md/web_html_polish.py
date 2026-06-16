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
)
from .web_polish.core import (
    WebArticleExtraction,
    WebHtmlKind,
    WebHtmlPolishError,
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


_IMG_SRC_RE = re.compile(
    r"(?P<prefix><img\b[^>]*?\ssrc\s*=\s*)(?P<quote>['\"])(?P<src>.*?)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)
_SRCSET_RE = re.compile(
    r"(?P<prefix><(?:img|source)\b[^>]*?\ssrcset\s*=\s*)(?P<quote>['\"])(?P<srcset>.*?)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)
_ROOT_RELATIVE_URL_ATTR_RE = re.compile(
    r"(?P<prefix>(?<![\w:-])(?P<name>href|src|action|poster)\s*=\s*)"
    r"(?P<quote>['\"])(?P<url>.*?)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)
_TITLE_RE = re.compile(r"<title\b[^>]*>(?P<title>[\s\S]*?)</title>", re.IGNORECASE)
_META_TAG_RE = re.compile(r"<meta\b(?P<attrs>[^>]*)>", re.IGNORECASE | re.DOTALL)
_H1_RE = re.compile(r"<h1\b", re.IGNORECASE)


def detect_web_html_kind(html: str, *, source_url: str | None = None) -> WebHtmlKind:
    """Classify known web HTML attachments."""

    parsed_source = _urlsplit_or_none(source_url)
    if parsed_source is not None and _arxiv_abs_parts(parsed_source) is not None:
        return WebHtmlKind.ARXIV_ABS_PAGE

    sample = html[:500_000].lower()
    if _looks_like_arxiv_abs_page(sample):
        return WebHtmlKind.ARXIV_ABS_PAGE
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
) -> WebHtmlPolishResult:
    """Normalize a web-native article HTML document.

    This keeps web-native sources separate from the Marker/PDF repair path:
    strip executable payloads, extract the article-like fragment, canonicalize
    same-document links, and wrap the result in a stable readable shell.
    """

    kind = require_web_article_html(html, source_url=source_url)
    title = _document_title(html)
    extraction = extract_web_article_fragment(html, kind=kind)
    normalized_html = normalize_web_article_fragment(
        extraction.html,
        kind=kind,
        source_url=source_url,
        canonical_url=canonical_url,
    )
    declared_urls = _declared_document_urls(html)
    inferred_canonical_url = canonical_url or (declared_urls[0] if declared_urls else None)
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
) -> WebHtmlFilePolishResult:
    """Polish a web-native HTML file and inline local sidecar images."""

    html = html_path.read_text(encoding="utf-8", errors="replace")
    document = polish_web_html_document(
        html,
        source_url=source_url,
        canonical_url=canonical_url,
    )
    inlined = inline_local_images_from_web_html_document(document.html, base_dir=html_path.parent)
    remote_inlined = inline_remote_images_from_web_html_document(
        inlined.html,
        allowed_hosts=_remote_image_hosts_for_kind(document.kind),
    )
    return WebHtmlFilePolishResult(
        html=remote_inlined.html,
        kind=document.kind,
        article_extracted=document.article_extracted,
        article_selector=document.article_selector,
        same_document_links_rewritten=document.same_document_links_rewritten,
        unresolved_same_document_links=document.unresolved_same_document_links,
        inlined_images=inlined.inlined_images + remote_inlined.inlined_images,
    )


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
) -> str:
    """Apply publisher-specific static normalizations after extraction."""

    return normalize_source_specific_article_fragment(
        html,
        kind=kind,
        source_url=source_url,
        canonical_url=canonical_url,
    )


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
        and ("abstract-content" in sample or "self.__next_f.push" in sample)
    )


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
    if not candidate.is_file():
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
