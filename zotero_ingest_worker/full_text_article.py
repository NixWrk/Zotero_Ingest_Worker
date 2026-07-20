from __future__ import annotations

import re
import urllib.parse
from typing import Any


SUBSTANTIVE_SECTIONS = frozenset({"methods", "results", "discussion", "conclusion"})
REFERENCE_SECTIONS = frozenset({"references"})
STRONG_ARTICLE_MARKERS = frozenset(
    {
        "article_tag",
        "article_body",
        "pmc_article",
        "schema_article",
        "arxiv_ltx_document",
        "arxiv_ltx_abstract",
        "arxiv_ltx_bibliography",
    }
)


def html_download_article_verdict(item: dict[str, Any]) -> dict[str, Any]:
    """Classify whether a downloaded HTML candidate is usable full article HTML."""
    article = item.get("article")
    article_data = article if isinstance(article, dict) else {}
    markers = _normalized_set(article_data.get("markers"))
    sections = _normalized_set(article_data.get("section_markers"))
    raw_text_chars = article_data.get("text_chars")
    text_chars = _int_value(raw_text_chars)
    kind = _normalized_text(item.get("kind")).casefold()
    title = _normalized_text(article_data.get("title"))
    title_lc = title.casefold()

    base = {
        "text_chars": text_chars,
        "markers": sorted(markers),
        "section_markers": sorted(sections),
    }

    if item.get("ok") is not True:
        return {
            "ok": False,
            "reason": str(item.get("status") or "download_not_ok"),
            **base,
        }
    if not _normalized_text(item.get("output_path")):
        return {"ok": False, "reason": "missing_output_path", **base}
    if is_arxiv_abs_landing_download(item):
        return {"ok": False, "reason": "arxiv_abs_landing", **base}
    if _is_unresolved_doi_landing(item):
        return {"ok": False, "reason": "doi_landing", **base}
    article_ok = article_data.get("ok")
    if article_ok is not None and article_ok is not True:
        reason = (
            str(article_data.get("reason") or "rejected")
            if article_ok is False
            else "invalid_ok"
        )
        return {
            "ok": False,
            "reason": f"article_validator_{reason}",
            **base,
        }

    if not _is_exact_nonnegative_int(raw_text_chars):
        return {
            "ok": False,
            "reason": "article_validator_invalid_text_chars",
            **base,
        }

    if _looks_like_access_landing(title_lc) and not _has_substantial_body(
        text_chars, markers, sections
    ):
        return {"ok": False, "reason": "access_landing", **base}

    if _is_arxiv_html_download(item):
        if text_chars >= 4_000 and markers.intersection(STRONG_ARTICLE_MARKERS):
            return {"ok": True, "reason": "arxiv_article_html", **base}
        return {"ok": False, "reason": "weak_arxiv_html", **base}

    if text_chars < 8_000:
        return {"ok": False, "reason": "short_text", **base}

    if kind == "landing":
        if not _has_substantial_body(text_chars, markers, sections):
            return {"ok": False, "reason": "weak_landing", **base}
        return {"ok": True, "reason": "article_landing_with_body", **base}

    if not _has_article_evidence(text_chars, markers, sections):
        return {"ok": False, "reason": "weak_article_evidence", **base}
    return {"ok": True, "reason": "article_html", **base}


def annotate_html_download_article_verdicts(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    annotated: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        verdict = html_download_article_verdict(item)
        item["article_verdict"] = verdict
        annotated.append(item)
    return annotated


def is_arxiv_abs_landing_download(item: dict[str, Any]) -> bool:
    for url in _candidate_urls(item):
        parsed_url = _parsed_http_url(url)
        if parsed_url is None:
            continue
        parsed, host = parsed_url
        if _is_host_or_subdomain(
            host, "arxiv.org"
        ) and parsed.path.casefold().startswith("/abs/"):
            return True
    return False


def arxiv_abs_ids_from_html_downloads(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        for url in _candidate_urls(item):
            arxiv_id = arxiv_id_from_abs_url(url)
            if not arxiv_id:
                continue
            key = arxiv_id.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(arxiv_id)
    return result


def arxiv_id_from_abs_url(url: str) -> str | None:
    parsed_url = _parsed_http_url(url)
    if parsed_url is None:
        return None
    parsed, host = parsed_url
    if not _is_host_or_subdomain(host, "arxiv.org"):
        return None
    parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0].casefold() != "abs":
        return None
    value = "/".join(parts[1:]).strip(" /")
    value = re.sub(r"v\d+\Z", "", value, flags=re.IGNORECASE)
    if (
        re.fullmatch(
            r"(?:[A-Za-z.-]+/\d{7}|\d{4}\.\d{4,5})",
            value,
        )
        is None
    ):
        return None
    return value


def _is_arxiv_html_download(item: dict[str, Any]) -> bool:
    source = _normalized_text(item.get("source")).casefold()
    if source == "arxiv":
        return True
    for url in _candidate_urls(item):
        parsed_url = _parsed_http_url(url)
        if parsed_url is None:
            continue
        parsed, host = parsed_url
        path = parsed.path.casefold()
        if _is_host_or_subdomain(host, "arxiv.org") and path.startswith("/html/"):
            return True
        if _is_host_or_subdomain(host, "ar5iv.labs.arxiv.org"):
            return True
    return False


def _is_unresolved_doi_landing(item: dict[str, Any]) -> bool:
    final_url = _normalized_text(item.get("final_url"))
    url = final_url or _normalized_text(item.get("url"))
    parsed_url = _parsed_http_url(url)
    if parsed_url is None:
        return False
    _parsed, host = parsed_url
    return host in {"doi.org", "dx.doi.org"}


def _has_substantial_body(
    text_chars: int, markers: set[str], sections: set[str]
) -> bool:
    if text_chars >= 30_000 and (
        markers.intersection(STRONG_ARTICLE_MARKERS) or sections
    ):
        return True
    if text_chars >= 15_000 and markers.intersection(STRONG_ARTICLE_MARKERS):
        return True
    if (
        text_chars >= 12_000
        and sections.intersection(SUBSTANTIVE_SECTIONS)
        and sections.intersection(REFERENCE_SECTIONS)
    ):
        return True
    if text_chars >= 20_000 and len(sections.intersection(SUBSTANTIVE_SECTIONS)) >= 2:
        return True
    return False


def _has_article_evidence(
    text_chars: int, markers: set[str], sections: set[str]
) -> bool:
    if markers.intersection(STRONG_ARTICLE_MARKERS):
        return True
    if text_chars >= 30_000 and sections.intersection(REFERENCE_SECTIONS):
        return True
    if text_chars >= 20_000 and len(sections.intersection(SUBSTANTIVE_SECTIONS)) >= 2:
        return True
    return False


def _looks_like_access_landing(title: str) -> bool:
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


def _candidate_urls(item: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for key in ("url", "final_url"):
        value = _normalized_text(item.get(key))
        if value:
            result.append(value)
    return result


def _normalized_set(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {
        item.strip().casefold()
        for item in value
        if isinstance(item, str) and item.strip()
    }


def _int_value(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _is_exact_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _normalized_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _parsed_http_url(
    value: object,
) -> tuple[urllib.parse.ParseResult, str] | None:
    text = _normalized_text(value)
    if not text:
        return None
    try:
        parsed = urllib.parse.urlparse(text)
        if parsed.scheme.casefold() not in {"http", "https"}:
            return None
        host = (parsed.hostname or "").rstrip(".").casefold()
    except (TypeError, ValueError):
        return None
    if not host:
        return None
    return parsed, host


def _is_host_or_subdomain(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")
