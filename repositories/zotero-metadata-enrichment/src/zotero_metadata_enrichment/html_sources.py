from __future__ import annotations

import hashlib
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field, replace
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, TypedDict

from .models import FullTextLocation
from .provider_http import throttled_urlopen
from .safe_http import UnsafeUrlError
from .text import normalize_space, strip_html, title_match_score
from .url_safety import validate_fetch_url


HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}
PDF_CONTENT_TYPES = {"application/pdf", "application/x-pdf"}
ARTICLE_MIN_TEXT_CHARS = 8_000
DEFAULT_MAX_ASSETS = 80
DEFAULT_MAX_ASSET_BYTES = 8_000_000
DEFAULT_MAX_TOTAL_ASSET_BYTES = 64_000_000


@dataclass(frozen=True)
class ArticleHtmlAssessment:
    ok: bool
    reason: str
    title: str = ""
    title_score: float = 0.0
    text_chars: int = 0
    image_count: int = 0
    link_count: int = 0
    markers: list[str] = field(default_factory=list)
    section_markers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _ArticleHtmlAssessmentFields(TypedDict):
    title: str
    title_score: float
    text_chars: int
    image_count: int
    link_count: int
    markers: list[str]
    section_markers: list[str]


@dataclass(frozen=True)
class HtmlSourceFetchResult:
    source: str
    url: str
    kind: str
    ok: bool
    status: str
    final_url: str = ""
    content_type: str = ""
    size: int = 0
    output_path: str = ""
    error: str = ""
    article: dict[str, Any] = field(default_factory=dict)
    assets: dict[str, Any] = field(default_factory=dict)
    derived_pdf_locations: list[FullTextLocation] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def download_html_sources(
    locations: list[FullTextLocation],
    *,
    output_dir: Path,
    limit: int = 5,
    timeout_seconds: int = 30,
    user_agent: str = "zotero-metadata-enrichment/0.1",
    max_bytes: int = 15_000_000,
    expected_title: str = "",
    save_assets: bool = True,
    max_assets: int = DEFAULT_MAX_ASSETS,
    stop_after_first_ok: bool = False,
) -> list[HtmlSourceFetchResult]:
    results: list[HtmlSourceFetchResult] = []
    attempts = 0
    for location in sorted(expanded_html_probe_locations(locations), key=_html_probe_priority):
        if not should_probe_for_html(location):
            results.append(
                HtmlSourceFetchResult(
                    source=location.source,
                    url=location.url,
                    kind=location.kind,
                    ok=False,
                    status="skipped_pdf" if is_pdf_location(location) else "skipped_kind",
                )
            )
            continue
        if limit > 0 and attempts >= limit:
            results.append(
                HtmlSourceFetchResult(
                    source=location.source,
                    url=location.url,
                    kind=location.kind,
                    ok=False,
                    status="skipped_limit",
                )
            )
            continue
        attempts += 1
        result = fetch_html_source(
            location,
            output_dir=output_dir,
            timeout_seconds=timeout_seconds,
            user_agent=user_agent,
            max_bytes=max_bytes,
            expected_title=expected_title,
            save_assets=save_assets,
            max_assets=max_assets,
            index=attempts,
        )
        results.append(result)
        if stop_after_first_ok and result.ok:
            break
    return results


def _html_probe_priority(location: FullTextLocation) -> tuple[int, int, int, str]:
    kind = location.kind.casefold()
    url = location.url.casefold()
    if not should_probe_for_html(location):
        return (3, 9, 9, url)
    direct_html_rank = 0 if kind == "html" else 1
    arxiv_rank = 2
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.casefold().split(":", 1)[0]
    if host.endswith("ar5iv.labs.arxiv.org"):
        arxiv_rank = 1
    elif host.endswith("arxiv.org") and parsed.path.casefold().startswith("/html/"):
        arxiv_rank = 0
    landing_rank = 1 if kind == "landing" else 0
    return (direct_html_rank, arxiv_rank, landing_rank, url)


def expanded_html_probe_locations(locations: list[FullTextLocation]) -> list[FullTextLocation]:
    expanded: list[FullTextLocation] = []
    for location in locations:
        expanded.extend(alternate_html_probe_locations(location))
        expanded.append(location)
    return dedupe_html_probe_locations(expanded)


def alternate_html_probe_locations(location: FullTextLocation) -> list[FullTextLocation]:
    alternates: list[FullTextLocation] = []
    arxiv_id = arxiv_id_from_html_candidate_url(location.url)
    if arxiv_id:
        alternates.extend(
            [
                FullTextLocation(
                    source="arxiv",
                    url=f"https://arxiv.org/html/{arxiv_id}",
                    kind="html",
                    is_oa=location.is_oa,
                    version=location.version,
                    content_type="text/html",
                    repository=location.repository,
                    raw={**(location.raw or {}), "derived_from": location.url, "derivation": "arxiv_html"},
                ),
                FullTextLocation(
                    source="ar5iv",
                    url=f"https://ar5iv.labs.arxiv.org/html/{arxiv_id}",
                    kind="html",
                    is_oa=location.is_oa,
                    version=location.version,
                    content_type="text/html",
                    repository="ar5iv",
                    raw={**(location.raw or {}), "derived_from": location.url, "derivation": "ar5iv_html"},
                ),
            ]
        )
    if location.kind.casefold() in {"landing", "webpage"} and looks_like_ojs_article_view_url(location.url):
        alternates.append(
            FullTextLocation(
                source=location.source,
                url=location.url,
                kind="html",
                is_oa=location.is_oa,
                version=location.version,
                content_type="text/html",
                repository=location.repository,
                raw={**(location.raw or {}), "derived_from": location.url, "derivation": "ojs_article_view"},
            )
        )
    return alternates


def dedupe_html_probe_locations(locations: list[FullTextLocation]) -> list[FullTextLocation]:
    result: list[FullTextLocation] = []
    seen: set[tuple[str, str, str]] = set()
    for location in locations:
        key = (
            location.source.casefold(),
            location.kind.casefold(),
            normalized_probe_url(location.url),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(location)
    return result


def normalized_probe_url(url: str) -> str:
    parsed = urllib.parse.urlparse(str(url or "").strip())
    return urllib.parse.urlunparse(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path,
            "",
            parsed.query,
            "",
        )
    )


def fetch_html_source(
    location: FullTextLocation,
    *,
    output_dir: Path,
    timeout_seconds: int = 30,
    user_agent: str = "zotero-metadata-enrichment/0.1",
    max_bytes: int = 15_000_000,
    expected_title: str = "",
    save_assets: bool = True,
    max_assets: int = DEFAULT_MAX_ASSETS,
    index: int = 1,
) -> HtmlSourceFetchResult:
    if is_pdf_location(location):
        return HtmlSourceFetchResult(
            source=location.source,
            url=location.url,
            kind=location.kind,
            ok=False,
            status="skipped_pdf",
        )
    safety = validate_fetch_url(location.url)
    if not safety.ok:
        return HtmlSourceFetchResult(
            source=location.source,
            url=location.url,
            kind=location.kind,
            ok=False,
            status="unsafe_url",
            error=safety.reason,
        )
    request = urllib.request.Request(
        location.url,
        headers={
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
            "User-Agent": user_agent,
        },
        method="GET",
    )
    try:
        with throttled_urlopen(request, timeout=timeout_seconds) as response:
            final_url = str(getattr(response, "url", location.url) or location.url)
            content_type = str(response.headers.get("Content-Type") or "")
            mime = content_type.split(";", 1)[0].strip().casefold()
            redirect_safety = validate_fetch_url(final_url)
            if not redirect_safety.ok:
                return HtmlSourceFetchResult(
                    source=location.source,
                    url=location.url,
                    kind=location.kind,
                    ok=False,
                    status="unsafe_redirect",
                    final_url=final_url,
                    content_type=content_type,
                    error=redirect_safety.reason,
                )
            byte_limit = max(max_bytes, 0)
            declared_bytes = _response_content_length(response.headers)
            if declared_bytes is not None and declared_bytes > byte_limit:
                return HtmlSourceFetchResult(
                    source=location.source,
                    url=location.url,
                    kind=location.kind,
                    ok=False,
                    status="too_large",
                    final_url=final_url,
                    content_type=content_type,
                    size=declared_bytes,
                )
            body = response.read(byte_limit + 1)
    except UnsafeUrlError as exc:
        return HtmlSourceFetchResult(
            source=location.source,
            url=location.url,
            kind=location.kind,
            ok=False,
            status="unsafe_redirect" if exc.is_redirect else "unsafe_url",
            error=str(exc),
        )
    except urllib.error.HTTPError as exc:
        return HtmlSourceFetchResult(
            source=location.source,
            url=location.url,
            kind=location.kind,
            ok=False,
            status="http_error",
            error=f"HTTP {exc.code}",
        )
    except Exception as exc:
        return HtmlSourceFetchResult(
            source=location.source,
            url=location.url,
            kind=location.kind,
            ok=False,
            status="fetch_error",
            error=str(exc),
        )

    if len(body) > max(max_bytes, 0):
        return HtmlSourceFetchResult(
            source=location.source,
            url=location.url,
            kind=location.kind,
            ok=False,
            status="too_large",
            final_url=final_url,
            content_type=content_type,
            size=len(body),
        )
    if mime in PDF_CONTENT_TYPES or is_probable_pdf_url(final_url):
        return HtmlSourceFetchResult(
            source=location.source,
            url=location.url,
            kind=location.kind,
            ok=False,
            status="rejected_pdf",
            final_url=final_url,
            content_type=content_type,
            size=len(body),
        )
    if mime not in HTML_CONTENT_TYPES and not looks_like_html(body):
        return HtmlSourceFetchResult(
            source=location.source,
            url=location.url,
            kind=location.kind,
            ok=False,
            status="non_html",
            final_url=final_url,
            content_type=content_type,
            size=len(body),
        )
    derived_pdf_locations = derived_pdf_locations_from_html_body(
        location,
        final_url=final_url,
        body=body,
    )
    canonical_html_url = canonical_article_html_url(location, final_url=final_url, body=body)
    if canonical_html_url:
        canonical_result = fetch_html_source(
            FullTextLocation(
                source=location.source,
                url=canonical_html_url,
                kind="html",
                is_oa=location.is_oa,
                version=location.version,
                content_type="text/html",
                repository=location.repository,
                raw={**(location.raw or {}), "canonicalized_from": final_url or location.url},
            ),
            output_dir=output_dir,
            timeout_seconds=timeout_seconds,
            user_agent=user_agent,
            max_bytes=max_bytes,
            expected_title=expected_title,
            save_assets=save_assets,
            max_assets=max_assets,
            index=index,
        )
        if derived_pdf_locations:
            return replace(
                canonical_result,
                derived_pdf_locations=dedupe_derived_pdf_locations(
                    [*canonical_result.derived_pdf_locations, *derived_pdf_locations]
                ),
            )
        return canonical_result
    profile = html_profile_for_location(location)
    article = assess_article_html(
        body,
        expected_title=expected_title,
        profile=profile,
    )
    if profile == "arxiv" and (
        is_arxiv_abs_landing_url(location.url) or is_arxiv_abs_landing_url(final_url)
    ):
        return HtmlSourceFetchResult(
            source=location.source,
            url=location.url,
            kind=location.kind,
            ok=False,
            status="arxiv_abs_landing",
            final_url=final_url,
            content_type=content_type,
            size=len(body),
            article=article.to_dict(),
            derived_pdf_locations=derived_pdf_locations,
        )
    effective_kind = resolved_html_kind(location, final_url=final_url)
    landing_rejection = landing_html_rejection_reason(
        location,
        final_url=final_url,
        article=article,
        effective_kind=effective_kind,
    )
    if landing_rejection:
        return HtmlSourceFetchResult(
            source=location.source,
            url=location.url,
            kind=effective_kind,
            ok=False,
            status=landing_rejection,
            final_url=final_url,
            content_type=content_type,
            size=len(body),
            article=article.to_dict(),
            derived_pdf_locations=derived_pdf_locations,
        )
    if not article.ok:
        return HtmlSourceFetchResult(
            source=location.source,
            url=location.url,
            kind=effective_kind,
            ok=False,
            status=article.reason,
            final_url=final_url,
            content_type=content_type,
            size=len(body),
            article=article.to_dict(),
            derived_pdf_locations=derived_pdf_locations,
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / html_source_filename(location, index=index)
    assets = write_html_snapshot(
        body,
        base_url=final_url,
        output_path=output_path,
        timeout_seconds=timeout_seconds,
        user_agent=user_agent,
        save_assets=save_assets,
        max_assets=max_assets,
    )
    return HtmlSourceFetchResult(
        source=location.source,
        url=location.url,
        kind=effective_kind,
        ok=True,
        status="downloaded",
        final_url=final_url,
        content_type=content_type,
        size=len(body),
        output_path=str(output_path),
        article=article.to_dict(),
        assets=assets,
        derived_pdf_locations=derived_pdf_locations,
    )


def should_probe_for_html(location: FullTextLocation) -> bool:
    if not location.url or is_pdf_location(location):
        return False
    return location.kind.casefold() in {"html", "landing", "fulltext", "webpage", "article", "doi"}


def resolved_html_kind(location: FullTextLocation, *, final_url: str) -> str:
    kind = location.kind.casefold()
    if kind not in {"landing", "doi", "webpage"}:
        return location.kind
    if looks_like_full_article_url(final_url):
        return "html"
    return location.kind


def landing_html_rejection_reason(
    location: FullTextLocation,
    *,
    final_url: str,
    article: ArticleHtmlAssessment,
    effective_kind: str,
) -> str:
    if effective_kind.casefold() == "html":
        if looks_like_access_landing(article.title):
            return "access_landing"
        return ""
    kind = location.kind.casefold()
    if kind not in {"landing", "doi", "webpage"}:
        return ""
    if looks_like_access_landing(article.title):
        return "access_landing"
    if looks_like_publisher_abstract_or_chapter(final_url):
        return "publisher_landing"
    if article.ok:
        return "weak_landing"
    return ""


def is_pdf_location(location: FullTextLocation) -> bool:
    content_type = location.content_type.casefold()
    return location.kind.casefold() == "pdf" or "pdf" in content_type or is_probable_pdf_url(location.url)


def is_probable_pdf_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.lower()
    return path.endswith(".pdf") or "/pdf/" in path or path.endswith("/pdf")


def derived_pdf_locations_from_html_body(
    location: FullTextLocation,
    *,
    final_url: str,
    body: bytes,
) -> list[FullTextLocation]:
    base_url = final_url or location.url
    result: list[FullTextLocation] = []
    for url in citation_pdf_urls_from_html(body, base_url=base_url):
        result.append(
            FullTextLocation(
                source=location.source,
                url=url,
                kind="pdf",
                is_oa=location.is_oa,
                content_type="application/pdf",
                repository=location.repository,
                raw={
                    **(location.raw or {}),
                    "derived_from": base_url,
                    "derivation": "citation_pdf_url",
                },
            )
        )

    iop_pdf_url = iop_pdf_url_from_article_url(base_url)
    if iop_pdf_url:
        result.append(
            FullTextLocation(
                source=location.source,
                url=iop_pdf_url,
                kind="pdf",
                is_oa=location.is_oa,
                content_type="application/pdf",
                repository=location.repository or "IOP Science",
                raw={
                    **(location.raw or {}),
                    "derived_from": base_url,
                    "derivation": "iop_article_pdf",
                },
            )
        )
    return dedupe_derived_pdf_locations(result)


def citation_pdf_urls_from_html(body: bytes, *, base_url: str) -> list[str]:
    parser = CitationPdfUrlParser()
    try:
        parser.feed(body.decode("utf-8", errors="replace"))
    except Exception:
        return []

    urls: list[str] = []
    seen: set[str] = set()
    for raw_url in parser.urls:
        absolute_url = urllib.parse.urljoin(base_url, raw_url).strip()
        if not absolute_url or absolute_url in seen or is_supplement_pdf_url(absolute_url):
            continue
        seen.add(absolute_url)
        urls.append(absolute_url)
    return urls


def iop_pdf_url_from_article_url(url: str) -> str:
    parsed = urllib.parse.urlparse(str(url or "").strip())
    host = parsed.netloc.casefold().split(":", 1)[0]
    path = parsed.path.rstrip("/")
    if not host.endswith("iopscience.iop.org"):
        return ""
    if not path.casefold().startswith("/article/") or path.casefold().endswith("/pdf"):
        return ""
    return urllib.parse.urlunparse(parsed._replace(path=f"{path}/pdf", query="", fragment=""))


def is_supplement_pdf_url(url: str) -> bool:
    path = urllib.parse.unquote(urllib.parse.urlparse(str(url or "")).path).casefold()
    filename = path.rsplit("/", 1)[-1]
    return "supplement" in filename


def dedupe_derived_pdf_locations(locations: list[FullTextLocation]) -> list[FullTextLocation]:
    result: list[FullTextLocation] = []
    seen: set[str] = set()
    for location in locations:
        url = normalized_probe_url(location.url)
        if not url or url in seen:
            continue
        seen.add(url)
        result.append(location)
    return result


def is_arxiv_abs_landing_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.casefold().split(":", 1)[0]
    path = parsed.path.casefold()
    return host.endswith("arxiv.org") and path.startswith("/abs/")


def arxiv_id_from_html_candidate_url(url: str) -> str:
    parsed = urllib.parse.urlparse(str(url or ""))
    host = parsed.netloc.casefold().split(":", 1)[0]
    parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return ""
    if host.endswith("arxiv.org") and parts[0].casefold() in {"abs", "html"}:
        return re.sub(r"v\d+\Z", "", parts[1].strip(), flags=re.IGNORECASE)
    if host.endswith("ar5iv.labs.arxiv.org") and parts[0].casefold() == "html":
        return re.sub(r"v\d+\Z", "", parts[1].strip(), flags=re.IGNORECASE)
    return ""


def canonical_article_html_url(location: FullTextLocation, *, final_url: str, body: bytes) -> str:
    parsed = urllib.parse.urlparse(final_url or location.url)
    host = parsed.netloc.casefold().split(":", 1)[0]
    path = parsed.path
    if host.endswith("thieme-connect.de") and "/products/ejournals/abstract/" in path:
        return urllib.parse.urlunparse(parsed._replace(path=path.replace("/abstract/", "/html/", 1), query=""))
    if host.endswith("thieme-connect.de"):
        raw = body.decode("utf-8", errors="replace")
        match = re.search(r"href=[\"']([^\"']*/products/ejournals/html/[^\"']+)[\"']", raw, flags=re.IGNORECASE)
        if match:
            return str(urllib.parse.urljoin(final_url or location.url, match.group(1))).strip()
    return ""


def looks_like_full_article_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(str(url or ""))
    host = parsed.netloc.casefold().split(":", 1)[0]
    path = parsed.path.casefold()
    if host.endswith("pmc.ncbi.nlm.nih.gov") and "/articles/" in path:
        return True
    if host.endswith("frontiersin.org") and path.endswith("/full"):
        return True
    if host.endswith("tandfonline.com") and path.startswith("/doi/full/"):
        return True
    if host.endswith("link.springer.com") and path.startswith("/article/"):
        return True
    if host.endswith("iopscience.iop.org") and path.startswith("/article/"):
        return True
    if host.endswith("ar5iv.labs.arxiv.org") and path.startswith("/html/"):
        return True
    if host.endswith("arxiv.org") and path.startswith("/html/"):
        return True
    if host.endswith("thieme-connect.de") and "/products/ejournals/html/" in path:
        return True
    if looks_like_ojs_article_view_url(url):
        return True
    return False


def looks_like_ojs_article_view_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(str(url or ""))
    path = parsed.path.casefold().rstrip("/")
    parts = [part for part in path.split("/") if part]
    if "article" not in parts or "view" not in parts:
        return False
    article_index = parts.index("article")
    return len(parts) > article_index + 2 and parts[article_index + 1] == "view"


def looks_like_access_landing(title: str) -> bool:
    title = str(title or "").casefold()
    markers = (
        "get access",
        "purchase access",
        "rent this article",
        "login",
        "log in",
        "subscribe",
        "institutional access",
    )
    return any(marker in title for marker in markers)


def looks_like_publisher_abstract_or_chapter(url: str) -> bool:
    parsed = urllib.parse.urlparse(str(url or ""))
    host = parsed.netloc.casefold().split(":", 1)[0]
    path = parsed.path.casefold()
    if host.endswith("thieme-connect.de") and "/products/ejournals/abstract/" in path:
        return True
    if host.endswith("link.springer.com") and path.startswith("/chapter/"):
        return True
    if host.endswith("pubs.acs.org") and path.startswith("/doi/"):
        return True
    return False


def looks_like_html(body: bytes) -> bool:
    prefix = body[:4096].lstrip().lower()
    return (
        prefix.startswith(b"<!doctype html")
        or prefix.startswith(b"<html")
        or b"<html" in prefix
        or b"<body" in prefix
    )


def assess_article_html(
    body: bytes,
    *,
    expected_title: str = "",
    profile: str = "generic",
) -> ArticleHtmlAssessment:
    raw = body.decode("utf-8", errors="replace")
    profile = normalize_html_profile(profile)
    lower = raw.casefold()
    title = html_title(body)
    text = visible_html_text(raw)
    title_score = title_match_score(expected_title, title) if expected_title and title else 0.0
    markers = article_markers(raw)
    section_markers = article_section_markers(text)
    image_count = len(re.findall(r"<img\b", lower))
    link_count = len(re.findall(r"\bhref\s*=", lower))
    base: _ArticleHtmlAssessmentFields = {
        "title": title,
        "title_score": title_score,
        "text_chars": len(text),
        "image_count": image_count,
        "link_count": link_count,
        "markers": markers,
        "section_markers": section_markers,
    }
    if is_challenge_html(title=title, text=text):
        return ArticleHtmlAssessment(ok=False, reason="challenge_page", **base)
    if is_limited_access_preview_html(title=title, text=text):
        return ArticleHtmlAssessment(ok=False, reason="limited_access_preview", **base)
    if expected_title and not html_matches_expected_title(body, expected_title):
        return ArticleHtmlAssessment(ok=False, reason="title_mismatch", **base)
    if len(text) < article_min_text_chars(profile):
        return ArticleHtmlAssessment(ok=False, reason="short_text", **base)
    if not has_article_structure(markers, profile=profile):
        return ArticleHtmlAssessment(ok=False, reason="article_structure_missing", **base)
    if not has_article_body_evidence(markers, section_markers, profile=profile):
        return ArticleHtmlAssessment(ok=False, reason="article_body_missing", **base)
    return ArticleHtmlAssessment(ok=True, reason="article_html", **base)


def html_profile_for_location(location: FullTextLocation) -> str:
    source = location.source.casefold()
    parsed = urllib.parse.urlparse(location.url)
    host = parsed.netloc.casefold()
    path = parsed.path.casefold()
    if "arxiv" in source or host.endswith("arxiv.org") or host.endswith("ar5iv.labs.arxiv.org"):
        return "arxiv"
    if (
        source == "pmc_oai"
        or host.endswith("pmc.ncbi.nlm.nih.gov")
        or (host.endswith("europepmc.org") and "/article/" in path and "pmc" in path)
    ):
        return "pmc"
    if host.endswith("iopscience.iop.org") and path.startswith("/article/"):
        return "iop"
    if source in {"core", "openaire", "doaj", "datacite", "unpaywall"}:
        return "repository"
    return "generic"


def normalize_html_profile(profile: str) -> str:
    profile = str(profile or "generic").casefold().strip()
    return profile if profile in {"generic", "arxiv", "pmc", "repository", "iop"} else "generic"


def article_min_text_chars(profile: str) -> int:
    if profile == "arxiv":
        return 4_000
    if profile == "pmc":
        return 5_000
    if profile == "iop":
        return 5_000
    return ARTICLE_MIN_TEXT_CHARS


def article_markers(raw: str) -> list[str]:
    lower = raw.casefold()
    patterns = [
        ("article_tag", r"<article\b"),
        ("citation_title", r"name=[\"']citation_title[\"']"),
        ("og_article", r"property=[\"']og:type[\"'][^>]+article"),
        ("schema_article", r"@type[^\n]{0,120}article"),
        ("abstract", r"\babstract\b"),
        ("references", r"\breferences\b"),
        ("figures", r"\bfigures?\b"),
        ("fulltext", r"full[- ]?text"),
        ("article_body", r"article[-_ ]?(body|content|section)"),
        ("pmc_article", r"pmc-article"),
        ("arxiv_ltx_document", r"\bltx_document\b"),
        ("arxiv_ltx_abstract", r"\bltx_abstract\b"),
        ("arxiv_ltx_bibliography", r"\bltx_bibliography\b"),
        ("arxiv_html", r"arxiv[^\"'>]{0,40}html"),
    ]
    return [name for name, pattern in patterns if re.search(pattern, lower)]


def article_section_markers(text: str) -> list[str]:
    lowered = text.casefold()
    sections = [
        ("abstract", r"\babstract\b"),
        ("methods", r"\b(methods|materials and methods)\b"),
        ("results", r"\bresults\b"),
        ("discussion", r"\bdiscussion\b"),
        ("conclusion", r"\bconclusions?\b"),
        ("references", r"\breferences\b"),
    ]
    return [name for name, pattern in sections if re.search(pattern, lowered)]


def has_article_structure(markers: list[str], *, profile: str = "generic") -> bool:
    strong = {"article_tag", "article_body", "pmc_article", "schema_article"}
    if profile == "arxiv":
        strong = strong | {"arxiv_ltx_document"}
    if profile == "pmc":
        strong = strong | {"pmc_article"}
    return bool(strong & set(markers))


def has_article_body_evidence(
    markers: list[str],
    section_markers: list[str],
    *,
    profile: str = "generic",
) -> bool:
    marker_set = set(markers)
    if profile == "arxiv" and {"arxiv_ltx_abstract", "arxiv_ltx_bibliography"} <= marker_set:
        return True
    if "references" in markers or "references" in section_markers:
        return True
    return len(set(section_markers) & {"methods", "results", "discussion", "conclusion"}) >= 2


def is_challenge_html(*, title: str, text: str) -> bool:
    haystack = f"{title}\n{text[:5000]}".casefold()
    markers = (
        "client challenge",
        "checking your browser",
        "recaptcha",
        "captcha",
        "cloudflare",
        "access denied",
        "just a moment",
        "enable javascript",
        "are you a human",
    )
    return any(marker in haystack for marker in markers)


def is_limited_access_preview_html(*, title: str, text: str) -> bool:
    haystack = f"{title}\n{text}".casefold()
    hard_markers = (
        "this is a preview of subscription content",
        "this is a preview of subscription content, log in via an institution",
    )
    if any(marker in haystack for marker in hard_markers):
        return True
    if "access this chapter" in haystack and (
        "buy chapter" in haystack
        or "subscribe and save" in haystack
        or "institutional subscriptions" in haystack
        or "log in via an institution" in haystack
    ):
        return True
    return False


def html_matches_expected_title(body: bytes, expected_title: str) -> bool:
    expected_title = normalize_space(expected_title)
    if not expected_title:
        return True
    text = html_text(body)
    if not text:
        return False
    title = html_title(body)
    if title and title_match_score(expected_title, title) >= 0.65:
        return True
    lowered = text.casefold()
    tokens = significant_title_tokens(expected_title)
    if not tokens:
        return True
    hits = sum(1 for token in tokens if token in lowered)
    return hits >= min(max(2, len(tokens) // 2), 5)


def html_title(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace")
    match = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    return normalize_space(strip_html(match.group(1))) if match else ""


def html_text(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace")
    return visible_html_text(text).casefold()


def visible_html_text(text: str) -> str:
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    return normalize_space(strip_html(text))


def significant_title_tokens(title: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9-]{3,}", title.casefold())
    stopwords = {
        "with",
        "from",
        "into",
        "that",
        "this",
        "using",
        "through",
        "towards",
        "between",
        "article",
    }
    result: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        if token in stopwords or token in seen:
            continue
        seen.add(token)
        result.append(token)
    return result[:10]


def html_source_filename(location: FullTextLocation, *, index: int) -> str:
    source = safe_filename_part(location.source or "source")
    parsed = urllib.parse.urlparse(location.url)
    host = safe_filename_part(parsed.netloc or "url")
    digest = hashlib.sha1(location.url.encode("utf-8")).hexdigest()[:10]
    return f"{index:02d}.{source}.{host}.{digest}.html"


def safe_filename_part(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return value.strip("._-")[:60] or "value"


def write_html_snapshot(
    body: bytes,
    *,
    base_url: str,
    output_path: Path,
    timeout_seconds: int,
    user_agent: str,
    save_assets: bool = True,
    max_assets: int = DEFAULT_MAX_ASSETS,
    max_total_asset_bytes: int = DEFAULT_MAX_TOTAL_ASSET_BYTES,
) -> dict[str, Any]:
    if not save_assets:
        output_path.write_bytes(body)
        return {"enabled": False, "saved": 0, "failed": 0, "assets_dir": ""}

    html_text_value = body.decode("utf-8", errors="replace")
    parser = ResourceReferenceParser()
    try:
        parser.feed(html_text_value)
    except Exception:
        pass
    document_base_url = document_resource_base_url(base_url, parser.base_href)

    assets_dir = output_path.parent / f"{output_path.stem}_assets"
    downloader = SnapshotAssetDownloader(
        assets_dir=assets_dir,
        timeout_seconds=timeout_seconds,
        user_agent=user_agent,
        max_assets=max_assets,
        max_total_bytes=max_total_asset_bytes,
    )
    replacements: dict[str, str] = {}
    for raw_url in parser.resource_urls:
        local = downloader.fetch(raw_url, base_url=document_base_url)
        if local:
            replacements[raw_url] = local

    rewritten = strip_base_tag(html_text_value)
    for raw_url, local in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
        rewritten = rewritten.replace(raw_url, local)
    output_path.write_text(rewritten, encoding="utf-8")
    return {
        "enabled": True,
        "saved": downloader.saved_count,
        "failed": downloader.failed_count,
        "assets_dir": str(assets_dir) if downloader.saved_count else "",
        "rewritten": len(replacements),
        "failures": downloader.failures[:20],
    }


class CitationPdfUrlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "meta":
            return
        attrs_dict = {name.casefold(): value or "" for name, value in attrs}
        name = attrs_dict.get("name", attrs_dict.get("property", "")).casefold().strip()
        if name != "citation_pdf_url":
            return
        url = attrs_dict.get("content", "").strip()
        if url:
            self.urls.append(url)


class ResourceReferenceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._entries: list[tuple[int, int, str]] = []
        self._seen: set[str] = set()
        self._order = 0
        self.base_href = ""
        self._embedded_media_depth = 0

    @property
    def resource_urls(self) -> list[str]:
        return [url for _, _, url in sorted(self._entries)]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {name.casefold(): value or "" for name, value in attrs}
        tag = tag.casefold()
        if tag == "base" and not self.base_href:
            self.base_href = attrs_dict.get("href", "").strip()
        if tag in {"audio", "video"}:
            self._embedded_media_depth += 1
            return
        if tag in {"img", "source"}:
            if self._embedded_media_depth:
                return
            for attr in ("src", "data-src", "data-original", "data-lazy-src"):
                self._add_image(attrs_dict.get(attr, ""), attrs=attrs_dict)
            for attr in ("srcset", "data-srcset"):
                for url in parse_srcset(attrs_dict.get(attr, "")):
                    self._add_image(url, attrs=attrs_dict)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"audio", "video"} and self._embedded_media_depth:
            self._embedded_media_depth -= 1

    def _add_image(self, url: str, *, attrs: dict[str, str]) -> None:
        if not should_download_snapshot_image(url, attrs=attrs):
            return
        self._add(url, priority=1)

    def _add(self, url: str, *, priority: int) -> None:
        url = str(url or "").strip()
        if not url or should_skip_asset_url(url):
            return
        if url in self._seen:
            return
        priority = min(priority, resource_priority(url))
        self._seen.add(url)
        self._order += 1
        self._entries.append((priority, self._order, url))


def document_resource_base_url(base_url: str, base_href: str) -> str:
    base_href = str(base_href or "").strip()
    if not base_href or should_skip_asset_url(base_href):
        return base_url
    return urllib.parse.urljoin(base_url, base_href)


def strip_base_tag(html_text_value: str) -> str:
    return re.sub(r"<base\b[^>]*>", "", html_text_value, count=1, flags=re.IGNORECASE)


class SnapshotAssetDownloader:
    def __init__(
        self,
        *,
        assets_dir: Path,
        timeout_seconds: int,
        user_agent: str,
        max_assets: int,
        max_total_bytes: int,
    ) -> None:
        self.assets_dir = assets_dir
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self.max_assets = max(max_assets, 0)
        self.max_total_bytes = max(max_total_bytes, 0)
        self.saved_count = 0
        self.failed_count = 0
        self.total_bytes = 0
        self.failures: list[dict[str, str]] = []
        self._seen: dict[str, str] = {}
        self._in_progress: set[str] = set()

    def fetch(self, raw_url: str, *, base_url: str, depth: int = 0) -> str | None:
        absolute_url = urllib.parse.urljoin(base_url, raw_url)
        if should_skip_asset_url(absolute_url):
            return None
        if absolute_url in self._seen:
            return self._seen[absolute_url]
        if absolute_url in self._in_progress:
            self.failed_count += 1
            self.failures.append({"url": absolute_url, "reason": "asset_cycle"})
            return None
        if depth > 3:
            self.failed_count += 1
            self.failures.append({"url": absolute_url, "reason": "asset_depth_limit"})
            return None
        if self.saved_count >= self.max_assets:
            self.failed_count += 1
            self.failures.append({"url": absolute_url, "reason": "asset_limit"})
            return None
        if self.total_bytes >= self.max_total_bytes:
            self.failed_count += 1
            self.failures.append({"url": absolute_url, "reason": "asset_total_bytes_limit"})
            return None
        safety = validate_fetch_url(absolute_url)
        if not safety.ok:
            self.failed_count += 1
            self.failures.append({"url": absolute_url, "reason": f"unsafe_url:{safety.reason}"})
            return None
        request = urllib.request.Request(
            absolute_url,
            headers={"Accept": "*/*", "User-Agent": self.user_agent},
            method="GET",
        )
        try:
            self._in_progress.add(absolute_url)
            with throttled_urlopen(request, timeout=self.timeout_seconds) as response:
                final_url = str(getattr(response, "url", absolute_url) or absolute_url)
                content_type = str(response.headers.get("Content-Type") or "")
                redirect_safety = validate_fetch_url(final_url)
                if not redirect_safety.ok:
                    self.failed_count += 1
                    self.failures.append(
                        {"url": absolute_url, "reason": f"unsafe_redirect:{redirect_safety.reason}"}
                    )
                    return None
                remaining_bytes = self.max_total_bytes - self.total_bytes
                read_limit = min(DEFAULT_MAX_ASSET_BYTES, remaining_bytes)
                declared_bytes = _response_content_length(response.headers)
                if declared_bytes is not None and declared_bytes > DEFAULT_MAX_ASSET_BYTES:
                    self.failed_count += 1
                    self.failures.append({"url": absolute_url, "reason": "asset_too_large"})
                    return None
                if declared_bytes is not None and declared_bytes > remaining_bytes:
                    self.failed_count += 1
                    self.failures.append({"url": absolute_url, "reason": "asset_total_bytes_limit"})
                    return None
                payload = response.read(read_limit + 1)
        except UnsafeUrlError as exc:
            self.failed_count += 1
            prefix = "unsafe_redirect" if exc.is_redirect else "unsafe_url"
            self.failures.append({"url": absolute_url, "reason": f"{prefix}:{exc}"})
            return None
        except Exception as exc:
            self.failed_count += 1
            self.failures.append({"url": absolute_url, "reason": str(exc)[:200]})
            return None
        finally:
            self._in_progress.discard(absolute_url)
        if len(payload) > read_limit:
            self.failed_count += 1
            reason = (
                "asset_too_large"
                if read_limit == DEFAULT_MAX_ASSET_BYTES
                else "asset_total_bytes_limit"
            )
            self.failures.append({"url": absolute_url, "reason": reason})
            return None

        self.assets_dir.mkdir(parents=True, exist_ok=True)
        rel_path = f"{asset_filename(self.saved_count + 1, final_url, content_type)}"
        target = self.assets_dir / rel_path
        local = f"{self.assets_dir.name}/{rel_path}"
        original_bytes = len(payload)
        self.saved_count += 1
        self.total_bytes += original_bytes
        if is_css_resource(final_url, content_type):
            self._in_progress.add(absolute_url)
            try:
                rewritten = self._rewrite_css_assets(payload, base_url=final_url, depth=depth + 1)
            finally:
                self._in_progress.discard(absolute_url)
            delta = len(rewritten) - original_bytes
            if delta <= 0 or self.total_bytes + delta <= self.max_total_bytes:
                payload = rewritten
                self.total_bytes += delta
            else:
                self.failed_count += 1
                self.failures.append(
                    {"url": absolute_url, "reason": "css_rewrite_total_bytes_limit"}
                )
        target.write_bytes(payload)
        self._seen[absolute_url] = local
        self._seen[final_url] = local
        return local

    def _rewrite_css_assets(self, payload: bytes, *, base_url: str, depth: int) -> bytes:
        css = payload.decode("utf-8", errors="replace")
        replacements: dict[str, str] = {}
        for raw_url in css_resource_references(css):
            local = self.fetch(raw_url, base_url=base_url, depth=depth)
            if local:
                replacements[raw_url] = Path(local).name
        for raw_url, local in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
            css = css.replace(raw_url, local)
        return css.encode("utf-8")


def _response_content_length(headers: Any) -> int | None:
    raw_value = headers.get("Content-Length") if headers is not None else None
    if raw_value is None:
        return None
    try:
        value = int(str(raw_value).strip())
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value


def parse_srcset(value: str) -> list[str]:
    urls: list[str] = []
    for part in re.split(r",\s+", str(value or "")):
        token = part.strip().split()
        if token:
            urls.append(token[0])
    return urls


def css_url_references(css: str) -> list[str]:
    urls: list[str] = []
    for match in re.finditer(r"url\(([^)]+)\)", css, flags=re.IGNORECASE):
        value = match.group(1).strip().strip("\"'")
        if value and not should_skip_asset_url(value):
            urls.append(value)
    return urls


def css_import_references(css: str) -> list[str]:
    urls: list[str] = []
    pattern = re.compile(
        r"@import\s+(?:url\(\s*)?[\"']?([^\"')\s;]+)",
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(css):
        value = match.group(1).strip()
        if value and not should_skip_asset_url(value):
            urls.append(value)
    return urls


def css_resource_references(css: str) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for value in css_url_references(css) + css_import_references(css):
        if value in seen:
            continue
        seen.add(value)
        urls.append(value)
    return urls


def should_skip_asset_url(url: str) -> bool:
    lowered = str(url or "").strip().casefold()
    return (
        not lowered
        or lowered.startswith(("data:", "javascript:", "mailto:", "#"))
        or lowered.endswith(".pdf")
    )


def should_download_snapshot_image(url: str, *, attrs: dict[str, str]) -> bool:
    url = str(url or "").strip()
    if not url or should_skip_asset_url(url):
        return False
    parsed = urllib.parse.urlparse(url)
    structural_probe = " ".join(
        (
            urllib.parse.unquote(f"{parsed.path}?{parsed.query}"),
            attrs.get("id", ""),
            attrs.get("class", ""),
            attrs.get("role", ""),
            attrs.get("data-testid", ""),
            attrs.get("style", ""),
        )
    ).casefold()
    semantic_probe = " ".join(
        (
            structural_probe,
            attrs.get("alt", ""),
            attrs.get("title", ""),
        )
    ).casefold()
    structural_tokens = set(re.sub(r"[^a-z0-9]+", " ", structural_probe).split())
    semantic_tokens = set(re.sub(r"[^a-z0-9]+", " ", semantic_probe).split())
    ui_tokens = {
        "analytics",
        "avatar",
        "badge",
        "button",
        "crossmark",
        "favicon",
        "icon",
        "loader",
        "loading",
        "logo",
        "pixel",
        "share",
        "social",
        "sprite",
        "spinner",
        "toolbar",
        "tracking",
    }
    semantic_ui_tokens = {
        "avatar",
        "crossmark",
        "favicon",
        "icon",
        "loader",
        "loading",
        "logo",
        "spinner",
    }
    if structural_tokens & ui_tokens or semantic_tokens & semantic_ui_tokens:
        return False
    scientific_tokens = (
        "chart",
        "diagram",
        "equation",
        "fig",
        "figure",
        "graph",
        "plot",
        "scheme",
    )
    has_scientific_marker = any(
        token.startswith(scientific_tokens) for token in semantic_tokens
    )
    suffix = Path(parsed.path).suffix.casefold()
    if suffix == ".svg" and not has_scientific_marker:
        return False
    if "hidden" in attrs and not has_scientific_marker:
        return False
    if attrs.get("aria-hidden", "").casefold() == "true" and not has_scientific_marker:
        return False
    if attrs.get("role", "").casefold() in {"none", "presentation"} and not has_scientific_marker:
        return False
    width = _numeric_html_dimension(attrs.get("width", ""))
    height = _numeric_html_dimension(attrs.get("height", ""))
    if not has_scientific_marker and (
        width is not None
        and height is not None
        and (width <= 1 or height <= 1 or width * height <= 16)
    ):
        return False
    return True


def _numeric_html_dimension(value: str) -> int | None:
    match = re.fullmatch(r"\s*(\d+)(?:\.0+)?(?:px)?\s*", str(value or ""), flags=re.IGNORECASE)
    return int(match.group(1)) if match is not None else None


def resource_priority(url: str) -> int:
    path = urllib.parse.urlparse(url).path.casefold()
    suffix = Path(path).suffix
    if any(marker in path for marker in ("/blobs/", "fig", "figure", "image")) and suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return 0
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return 1
    if suffix == ".css":
        return 2
    if suffix == ".svg" or "icon" in path or "logo" in path:
        return 4
    return 3


def is_css_resource(url: str, content_type: str) -> bool:
    return "text/css" in content_type.casefold() or urllib.parse.urlparse(url).path.lower().endswith(".css")


def asset_filename(index: int, url: str, content_type: str) -> str:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path
    suffix = Path(path).suffix.lower()
    if not suffix or len(suffix) > 8:
        suffix = suffix_for_content_type(content_type)
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    return f"{index:03d}.{digest}{suffix or '.bin'}"


def suffix_for_content_type(content_type: str) -> str:
    mime = content_type.split(";", 1)[0].strip().casefold()
    mapping = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/svg+xml": ".svg",
        "text/css": ".css",
        "font/woff": ".woff",
        "font/woff2": ".woff2",
        "application/font-woff": ".woff",
    }
    return mapping.get(mime, ".bin")
