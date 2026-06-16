"""Core types and HTML-fragment helpers for web-native polish."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from html import escape as html_escape
from html import unescape
import re


class WebHtmlKind(str, Enum):
    """Known web HTML source kinds."""

    ARXIV_ABS_PAGE = "arxiv_abs_page"
    ARXIV_LATEXML = "arxiv_latexml"
    PMC_ARTICLE = "pmc_article"
    TAYLOR_FRANCIS_ARTICLE = "taylor_francis_article"
    SPRINGER_NATURE_ARTICLE = "springer_nature_article"
    IOP_ARTICLE = "iop_article"
    RESEARCHGATE_PAGE = "researchgate_page"
    SCIENDO_ABSTRACT_PAGE = "sciendo_abstract_page"
    OJS_ABSTRACT_PAGE = "ojs_abstract_page"
    GENERIC_ARTICLE = "generic_article"
    UNKNOWN = "unknown"


class WebHtmlPolishError(ValueError):
    """Raised when a web HTML attachment cannot be polished as an article."""


@dataclass(frozen=True)
class WebArticleExtraction:
    html: str
    extracted: bool
    selector: str | None
    text_length: int


@dataclass(frozen=True)
class ArticleCandidate:
    html: str
    tag: str
    attrs: str
    score: int
    selector: str
    text_length: int


ARTICLE_START_RE = re.compile(
    r"<(?P<tag>article|main|section|div)\b(?P<attrs>[^<>]*)>",
    re.IGNORECASE,
)
HTML_TAG_RE = re.compile(r"</?(?P<tag>[A-Za-z][A-Za-z0-9:-]*)(?P<attrs>[^<>]*)?>", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")
NON_ARTICLE_BLOCK_RE = re.compile(
    r"<(?:script|style|noscript|template)\b[\s\S]*?</(?:script|style|noscript|template)>",
    re.IGNORECASE,
)
BODY_RE = re.compile(r"<body\b[^>]*>(?P<body>[\s\S]*?)</body>", re.IGNORECASE)
VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


def extract_generic_web_article_fragment(html: str, *, kind: WebHtmlKind | None = None) -> WebArticleExtraction:
    """Extract an article-like fragment without publisher-specific rules."""

    cleaned = strip_non_article_payloads(html)
    candidates: list[ArticleCandidate] = []
    for match in ARTICLE_START_RE.finditer(cleaned):
        tag = match.group("tag")
        attrs = match.group("attrs")
        if not promising_article_start(tag, attrs, kind=kind):
            continue
        fragment = balanced_element_from_match(cleaned, match)
        if fragment is None:
            continue
        candidate = score_article_candidate(fragment, tag, attrs, kind=kind)
        if candidate is not None:
            candidates.append(candidate)

    if candidates:
        best = max(candidates, key=lambda candidate: (candidate.score, candidate.text_length))
        if best.score >= 1_500 and best.text_length >= 1_000:
            return WebArticleExtraction(
                html=best.html.strip(),
                extracted=True,
                selector=best.selector,
                text_length=best.text_length,
            )

    body = body_inner(cleaned) or cleaned
    text_length = visible_text_length(body)
    return WebArticleExtraction(
        html=body.strip(),
        extracted=False,
        selector="body" if body != cleaned else None,
        text_length=text_length,
    )


def extract_fragment_by_attr_tokens(
    html: str,
    *,
    kind: WebHtmlKind,
    token_selectors: tuple[tuple[str, str], ...],
    min_text_length: int = 500,
) -> WebArticleExtraction | None:
    """Extract the best balanced element whose opening attrs match known tokens."""

    cleaned = strip_non_article_payloads(html)
    candidates: list[ArticleCandidate] = []
    for token, selector in token_selectors:
        token_lower = token.lower()
        for match in ARTICLE_START_RE.finditer(cleaned):
            tag = match.group("tag")
            attrs = match.group("attrs")
            attrs_lower = unescape(attrs).lower()
            if token_lower not in attrs_lower:
                continue
            fragment = balanced_element_from_match(cleaned, match)
            if fragment is None:
                continue
            candidate = score_article_candidate(fragment, tag, attrs, kind=kind)
            if candidate is None:
                continue
            candidates.append(
                ArticleCandidate(
                    html=candidate.html,
                    tag=candidate.tag,
                    attrs=candidate.attrs,
                    score=candidate.score + 50_000 - len(candidates),
                    selector=selector,
                    text_length=candidate.text_length,
                )
            )

    if not candidates:
        return None
    best = max(candidates, key=lambda candidate: (candidate.score, candidate.text_length))
    if best.text_length < min_text_length:
        return None
    return WebArticleExtraction(
        html=best.html.strip(),
        extracted=True,
        selector=best.selector,
        text_length=best.text_length,
    )


def remove_elements_by_attr_tokens(
    html: str,
    tokens: tuple[str, ...],
    *,
    tags: tuple[str, ...] = ("aside", "div", "footer", "header", "nav", "section"),
) -> str:
    """Remove balanced elements whose opening tag attributes contain any token."""

    lowered_tokens = tuple(token.lower() for token in tokens)
    allowed_tags = {tag.lower() for tag in tags}
    previous = None
    cleaned = html
    while previous != cleaned:
        previous = cleaned
        for match in list(HTML_TAG_RE.finditer(cleaned)):
            tag = match.group("tag").lower()
            if tag not in allowed_tags or match.group(0).startswith("</"):
                continue
            attrs = unescape(match.group("attrs") or "").lower()
            if not any(token in attrs for token in lowered_tokens):
                continue
            fragment = balanced_element_from_match(cleaned, match)
            if fragment is None:
                continue
            cleaned = cleaned[: match.start()] + " " + cleaned[match.start() + len(fragment) :]
            break
    return cleaned


def attr_value(attrs: str, name: str) -> str | None:
    match = re.search(
        rf"(?<![\w:-]){re.escape(name)}\s*=\s*(['\"])(?P<value>.*?)\1",
        attrs,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        return None
    return unescape(match.group("value")).strip()


def set_attr_value(open_tag: str, name: str, value: str) -> str:
    escaped_value = html_escape(value, quote=True)
    attr_re = re.compile(
        rf"(?P<prefix>(?<![\w:-]){re.escape(name)}\s*=\s*)(?P<quote>['\"])(?P<value>.*?)(?P=quote)",
        re.IGNORECASE | re.DOTALL,
    )
    if attr_re.search(open_tag):
        return attr_re.sub(
            lambda match: f"{match.group('prefix')}{match.group('quote')}{escaped_value}{match.group('quote')}",
            open_tag,
            count=1,
        )
    return re.sub(r">\s*$", f' {name}="{escaped_value}">', open_tag, count=1)


def strip_non_article_payloads(html: str) -> str:
    cleaned = COMMENT_RE.sub(" ", html)
    previous = None
    while cleaned != previous:
        previous = cleaned
        cleaned = NON_ARTICLE_BLOCK_RE.sub(" ", cleaned)
    return cleaned


def body_inner(html: str) -> str | None:
    match = BODY_RE.search(html)
    if match is None:
        return None
    return match.group("body")


def balanced_element_from_match(html: str, start_match: re.Match[str]) -> str | None:
    tag = start_match.group("tag").lower()
    depth = 0
    for match in HTML_TAG_RE.finditer(html, start_match.start()):
        token_tag = match.group("tag").lower()
        if token_tag != tag:
            continue
        raw = match.group(0)
        if raw.startswith("</"):
            depth -= 1
            if depth == 0:
                return html[start_match.start() : match.end()]
            continue
        if raw.endswith("/>") or token_tag in VOID_TAGS:
            continue
        depth += 1
    return None


def score_article_candidate(
    fragment: str,
    tag: str,
    attrs: str,
    *,
    kind: WebHtmlKind | None,
) -> ArticleCandidate | None:
    text_length = visible_text_length(fragment)
    if text_length < 500:
        return None

    tag = tag.lower()
    attrs_lower = unescape(attrs).lower()
    fragment_probe = fragment[:300_000].lower()
    score = min(text_length // 75, 3_000)
    selector = tag

    if "ltx_page_main" in attrs_lower:
        score += 8_000
        selector = ".ltx_page_main"
    if "pmc-article" in attrs_lower:
        score += 7_000
        selector = ".pmc-article"
    if "nlm_article" in attrs_lower:
        score += 6_500
        selector = ".NLM_article"
    if "c-article-body" in attrs_lower:
        score += 5_800
        selector = ".c-article-body"
    elif "article__body" in attrs_lower or "article-body" in attrs_lower:
        score += 5_500
        selector = ".article-body"
    if "article-content" in attrs_lower or "article__content" in attrs_lower:
        score += 5_000
        selector = ".article-content"
    if "wd-jnl-art-full-text" in attrs_lower:
        score += 5_800
        selector = ".wd-jnl-art-full-text"
    if 'itemprop="articlebody"' in attrs_lower or "itemprop='articlebody'" in attrs_lower:
        score += 5_000
        selector = '[itemprop="articleBody"]'
    if "hlfld-fulltext" in attrs_lower:
        score += 3_200
        selector = ".hlFld-Fulltext"
    if 'id="main-content"' in attrs_lower or "'main-content'" in attrs_lower:
        score += 3_000
        selector = "#main-content"

    if tag == "article":
        score += 3_000
        selector = "article" if selector == tag else selector
    elif tag == "main":
        score += 1_800
        selector = "main" if selector == tag else selector

    if tag == "article" and "pmc-article" in fragment_probe:
        score += 3_000
        selector = "article .pmc-article"
    if tag == "article" and "c-article-body" in fragment_probe:
        score += 2_500
        selector = "article .c-article-body"
    if tag == "article" and "nlm_article" in fragment_probe:
        score += 2_500
        selector = "article .NLM_article"
    if "ltx_bibliography" in fragment_probe or "references" in fragment_probe or "bibliography" in fragment_probe:
        score += 600

    if "abstract" in attrs_lower and "fulltext" not in attrs_lower and text_length < 8_000:
        score -= 2_500
    if any(word in attrs_lower for word in ("navbar", "navigation", "footer", "header", "sidebar", "cookie")):
        score -= 3_000

    if kind == WebHtmlKind.ARXIV_LATEXML and "ltx_page_main" not in attrs_lower:
        score -= 1_000
    if kind == WebHtmlKind.RESEARCHGATE_PAGE and text_length < 12_000:
        score -= 1_500

    return ArticleCandidate(
        html=fragment,
        tag=tag,
        attrs=attrs,
        score=score,
        selector=selector,
        text_length=text_length,
    )


def promising_article_start(tag: str, attrs: str, *, kind: WebHtmlKind | None) -> bool:
    tag = tag.lower()
    if tag in {"article", "main"}:
        return True

    attrs_lower = unescape(attrs).lower()
    if not attrs_lower:
        return False

    strong_tokens = (
        "ltx_page_main",
        "pmc-article",
        "nlm_article",
        "c-article-body",
        "article__body",
        "article-body",
        "article__content",
        "article-content",
        "wd-jnl-art-full-text",
        "articlebody",
        "hlfld-fulltext",
        "main-content",
        "fulltext-view",
        "article-section",
    )
    if any(token in attrs_lower for token in strong_tokens):
        return True

    if kind == WebHtmlKind.ARXIV_LATEXML and "ltx_" in attrs_lower:
        return True
    if tag == "section" and "jats" in attrs_lower and "article" in attrs_lower:
        return True
    return False


def visible_text_length(html: str) -> int:
    return len(visible_text(html))


def visible_text(html: str) -> str:
    text = TAG_RE.sub(" ", html)
    return " ".join(unescape(text).split())


# Backward-compatible private aliases for existing publisher modules and tests.
_ArticleCandidate = ArticleCandidate
_ARTICLE_START_RE = ARTICLE_START_RE
_HTML_TAG_RE = HTML_TAG_RE
_TAG_RE = TAG_RE
_COMMENT_RE = COMMENT_RE
_NON_ARTICLE_BLOCK_RE = NON_ARTICLE_BLOCK_RE
_VOID_TAGS = VOID_TAGS
_extract_fragment_by_attr_tokens = extract_fragment_by_attr_tokens
_remove_elements_by_attr_tokens = remove_elements_by_attr_tokens
_attr_value = attr_value
_set_attr_value = set_attr_value
_strip_non_article_payloads = strip_non_article_payloads
_body_inner = body_inner
_balanced_element_from_match = balanced_element_from_match
_score_article_candidate = score_article_candidate
_promising_article_start = promising_article_start
_visible_text_length = visible_text_length
_visible_text = visible_text
