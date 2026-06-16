"""Shared HTML link and fragment helpers.

The functions here are intentionally source-agnostic.  Raw/Marker polish,
web-native polish, and quality audits all need the same conservative rules for
same-document fragment links; keeping them outside a stage-specific module
prevents accidental coupling between those pipelines.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape as html_escape
from html import unescape
import re
import urllib.parse


@dataclass(frozen=True)
class SameDocumentLinkCanonicalization:
    html: str
    rewritten_count: int
    unresolved_count: int
    candidate_document_urls: tuple[str, ...]


ATTR_HREF_RE = re.compile(
    r"(?P<prefix>(?<![\w:-])href\s*=\s*)(?P<quote>['\"])(?P<href>.*?)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)
HREF_VALUE_RE = re.compile(
    r"<a\b[^>]*(?<![\w:-])href\s*=\s*(['\"])(?P<href>.*?)\1",
    re.IGNORECASE | re.DOTALL,
)
DOUBLE_QUOTED_HREF_ATTR_LITERAL_RE = re.compile(r'\bhref\s*=\s*"(?P<href>[^"]+)"', re.IGNORECASE)
SINGLE_QUOTED_HREF_ATTR_LITERAL_RE = re.compile(r"\bhref\s*=\s*'(?P<href>[^']+)'", re.IGNORECASE)
DECLARED_URL_RE = re.compile(
    r"<(?:link|meta)\b(?P<attrs>[^>]*)>",
    re.IGNORECASE | re.DOTALL,
)
ID_VALUE_RE = re.compile(r"(?<![\w:-])id\s*=\s*(['\"])(?P<id>.*?)\1", re.IGNORECASE | re.DOTALL)
NAME_VALUE_RE = re.compile(r"(?<![\w:-])name\s*=\s*(['\"])(?P<name>.*?)\1", re.IGNORECASE | re.DOTALL)
ARXIV_HTML_PATH_RE = re.compile(
    r"^/html/(?P<id>\d{4}\.\d{4,5})(?:v(?P<version>\d+))?/?$",
    re.IGNORECASE,
)
ARXIV_ABS_PATH_RE = re.compile(
    r"^/abs/(?P<id>\d{4}\.\d{4,5})(?:v(?P<version>\d+))?/?$",
    re.IGNORECASE,
)


def canonicalize_same_document_links(
    html: str,
    *,
    source_url: str | None = None,
    canonical_url: str | None = None,
    require_fragment_target: bool = True,
) -> SameDocumentLinkCanonicalization:
    """Rewrite absolute same-document fragment links to local fragments."""

    ids = extract_html_fragment_targets(html)
    candidates = document_url_candidates(
        html,
        ids=ids,
        source_url=source_url,
        canonical_url=canonical_url,
    )
    rewritten_count = 0
    unresolved_count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal rewritten_count, unresolved_count
        href = unescape(match.group("href")).strip()
        parsed = urlsplit_or_none(href)
        if parsed is None or not parsed.fragment:
            return match.group(0)
        if is_plain_local_fragment(parsed):
            target = urllib.parse.unquote(parsed.fragment)
            if require_fragment_target and ids and target not in ids:
                unresolved_count += 1
            return match.group(0)
        if not candidates:
            return match.group(0)
        if not is_same_document(parsed, candidates):
            return match.group(0)

        target = urllib.parse.unquote(parsed.fragment)
        if require_fragment_target and ids and target not in ids:
            unresolved_count += 1
            return match.group(0)

        rewritten_count += 1
        quote = match.group("quote")
        local_href = html_escape(f"#{parsed.fragment}", quote=True)
        return f"{match.group('prefix')}{quote}{local_href}{quote}"

    return SameDocumentLinkCanonicalization(
        html=ATTR_HREF_RE.sub(replace, html),
        rewritten_count=rewritten_count,
        unresolved_count=unresolved_count,
        candidate_document_urls=tuple(base_url_from_parsed(candidate) for candidate in candidates),
    )


def count_same_document_absolute_fragment_links(
    html: str,
    *,
    source_url: str | None = None,
    canonical_url: str | None = None,
) -> int:
    """Count absolute links that still point to this document's fragments."""

    ids = extract_html_fragment_targets(html)
    candidates = document_url_candidates(
        html,
        ids=ids,
        source_url=source_url,
        canonical_url=canonical_url,
    )
    if not candidates:
        return 0

    count = 0
    for match in HREF_VALUE_RE.finditer(html):
        href = unescape(match.group("href")).strip()
        parsed = urlsplit_or_none(href)
        if parsed is None or not parsed.fragment:
            continue
        if is_plain_local_fragment(parsed):
            continue
        if not is_same_document(parsed, candidates):
            continue
        target = urllib.parse.unquote(parsed.fragment)
        if ids and target not in ids:
            continue
        count += 1
    return count


def extract_html_fragment_targets(html: str) -> set[str]:
    targets = {unescape(match.group("id")) for match in ID_VALUE_RE.finditer(html)}
    targets.update(unescape(match.group("name")) for match in NAME_VALUE_RE.finditer(html))
    return {target for target in targets if target}


def declared_document_urls(html: str) -> tuple[str, ...]:
    """Collect canonical/full-html URLs declared in the document head."""

    urls: list[str] = []
    for match in DECLARED_URL_RE.finditer(html[:500_000]):
        attrs = match.group("attrs") or ""
        href = attr_value(attrs, "href")
        content = attr_value(attrs, "content")
        rel = attr_value(attrs, "rel") or ""
        name = (attr_value(attrs, "name") or attr_value(attrs, "property") or "").lower()
        candidate: str | None = None
        if href and "canonical" in rel.lower():
            candidate = href
        elif content and name in {
            "citation_full_html_url",
            "citation_public_url",
            "citation_abstract_html_url",
            "og:url",
        }:
            candidate = content
        if candidate and candidate.startswith(("http://", "https://")):
            urls.append(candidate)
    return tuple(dict.fromkeys(urls))


def document_url_candidates(
    html: str,
    *,
    ids: set[str],
    source_url: str | None,
    canonical_url: str | None,
) -> tuple[urllib.parse.SplitResult, ...]:
    explicit: list[urllib.parse.SplitResult] = []
    for raw_url in (source_url, canonical_url, *declared_document_urls(html)):
        parsed = urlsplit_or_none(raw_url)
        if parsed is not None and parsed.scheme and parsed.netloc:
            explicit.append(parsed._replace(fragment=""))
    if explicit:
        return tuple(dict.fromkeys(explicit))

    arxiv_counts: dict[tuple[str, str | None], int] = {}
    arxiv_parsed: dict[tuple[str, str | None], urllib.parse.SplitResult] = {}
    generic_counts: dict[tuple[str, str, str, str], int] = {}
    generic_parsed: dict[tuple[str, str, str, str], urllib.parse.SplitResult] = {}
    for match in HREF_VALUE_RE.finditer(html):
        href = unescape(match.group("href")).strip()
        parsed = urlsplit_or_none(href)
        if parsed is None or not parsed.fragment or not parsed.scheme or not parsed.netloc:
            continue
        target = urllib.parse.unquote(parsed.fragment)
        if ids and target not in ids:
            continue
        parts = arxiv_html_parts(parsed)
        if parts is not None:
            arxiv_id, version = parts
            key = (arxiv_id, version)
            arxiv_counts[key] = arxiv_counts.get(key, 0) + 1
            arxiv_parsed[key] = parsed._replace(fragment="")
        base = parsed._replace(fragment="")
        base_key = base_key_for_url(base)
        generic_counts[base_key] = generic_counts.get(base_key, 0) + 1
        generic_parsed[base_key] = base

    if arxiv_counts:
        top_count = max(arxiv_counts.values())
        if top_count >= 2:
            top_id = max(arxiv_counts.items(), key=lambda item: item[1])[0][0]
            return tuple(
                parsed
                for key, parsed in arxiv_parsed.items()
                if key[0] == top_id and arxiv_counts.get(key, 0) == top_count
            )

    if not generic_counts:
        return ()

    top_count = max(generic_counts.values())
    if top_count < 2:
        return ()
    return tuple(
        parsed
        for key, parsed in generic_parsed.items()
        if generic_counts.get(key, 0) == top_count
    )


def attr_value(attrs: str, name: str) -> str | None:
    match = re.search(
        rf"(?<![\w:-]){re.escape(name)}\s*=\s*(['\"])(?P<value>.*?)\1",
        attrs,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        return None
    return unescape(match.group("value")).strip()


def href_attr_literal(attrs: str) -> str | None:
    for pattern in (DOUBLE_QUOTED_HREF_ATTR_LITERAL_RE, SINGLE_QUOTED_HREF_ATTR_LITERAL_RE):
        match = pattern.search(attrs)
        if match is not None:
            return match.group("href")
    return None


def escape_html_attr_literal(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def replace_href_attr_literal(attrs: str, href: str) -> str:
    return re.sub(
        r"(\bhref\s*=\s*)(['\"])(.*?)\2",
        lambda match: f'{match.group(1)}"{escape_html_attr_literal(href)}"',
        attrs,
        count=1,
        flags=re.IGNORECASE | re.DOTALL,
    )


def is_same_document(
    parsed_href: urllib.parse.SplitResult,
    candidates: tuple[urllib.parse.SplitResult, ...],
) -> bool:
    for candidate in candidates:
        if relative_same_document(parsed_href, candidate):
            return True
        if same_arxiv_html_document(parsed_href, candidate):
            return True
        if base_key_for_url(parsed_href) == base_key_for_url(candidate):
            return True
    return False


def is_plain_local_fragment(parsed: urllib.parse.SplitResult) -> bool:
    return not parsed.scheme and not parsed.netloc and not parsed.path and not parsed.query and bool(parsed.fragment)


def is_root_relative_url(value: str) -> bool:
    return value.startswith("/") and not value.startswith("//")


def relative_same_document(
    parsed_href: urllib.parse.SplitResult,
    candidate: urllib.parse.SplitResult,
) -> bool:
    if parsed_href.scheme or parsed_href.netloc:
        return False
    if not parsed_href.fragment:
        return False
    if not parsed_href.path:
        return bool(parsed_href.query)
    return parsed_href.path.rstrip("/") == candidate.path.rstrip("/")


def same_arxiv_html_document(
    left: urllib.parse.SplitResult,
    right: urllib.parse.SplitResult,
) -> bool:
    left_parts = arxiv_html_parts(left)
    right_parts = arxiv_html_parts(right)
    if left_parts is None or right_parts is None:
        return False
    left_id, left_version = left_parts
    right_id, right_version = right_parts
    if left_id != right_id:
        return False
    return left_version == right_version or left_version is None or right_version is None


def arxiv_html_parts(parsed: urllib.parse.SplitResult) -> tuple[str, str | None] | None:
    if parsed.netloc.lower() not in {"arxiv.org", "www.arxiv.org"}:
        return None
    match = ARXIV_HTML_PATH_RE.match(parsed.path)
    if match is None:
        return None
    return match.group("id"), match.group("version")


def arxiv_abs_parts(parsed: urllib.parse.SplitResult) -> tuple[str, str | None] | None:
    if parsed.netloc.lower() not in {"arxiv.org", "www.arxiv.org"}:
        return None
    match = ARXIV_ABS_PATH_RE.match(parsed.path)
    if match is None:
        return None
    return match.group("id"), match.group("version")


def base_key_for_url(parsed: urllib.parse.SplitResult) -> tuple[str, str, str, str]:
    path = parsed.path.rstrip("/") or "/"
    return (parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query)


def base_url_from_parsed(parsed: urllib.parse.SplitResult) -> str:
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def urlsplit_or_none(raw_url: str | None) -> urllib.parse.SplitResult | None:
    if raw_url is None:
        return None
    value = unescape(str(raw_url)).strip()
    if not value:
        return None
    try:
        return urllib.parse.urlsplit(value)
    except ValueError:
        return None


# Backward-compatible private aliases used by existing publisher modules.
_ATTR_HREF_RE = ATTR_HREF_RE
_html_fragment_targets = extract_html_fragment_targets
_arxiv_html_parts = arxiv_html_parts
_arxiv_abs_parts = arxiv_abs_parts
_is_root_relative_url = is_root_relative_url
_urlsplit_or_none = urlsplit_or_none
