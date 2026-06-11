from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from ..identifiers import normalize_doi
from ..models import FullTextLocation, MetadataCandidate
from ..text import first_text, strip_html, title_match_score
from .common import candidate_with_locations


class CrossrefClient:
    def __init__(
        self,
        *,
        mailto: str = "",
        timeout_seconds: int = 60,
        user_agent: str = "zotero-metadata-enrichment/0.1",
    ) -> None:
        self.mailto = mailto
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent

    def by_doi(self, doi: str) -> MetadataCandidate | None:
        doi = normalize_doi(doi)
        if not doi:
            return None
        params = {"mailto": self.mailto} if self.mailto else {}
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        url = f"https://api.crossref.org/works/{urllib.parse.quote(doi, safe='')}{query}"
        payload = self._get_json(url)
        message = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(message, dict):
            return None
        return crossref_work_to_candidate(message, score=1.0)

    def by_title(self, title: str, *, rows: int = 5) -> MetadataCandidate | None:
        params = {
            "query.title": title,
            "rows": str(rows),
            "select": (
                "DOI,title,container-title,short-container-title,published-print,"
                "published-online,issued,ISSN,volume,issue,page,URL,abstract,author,type,link"
            ),
        }
        if self.mailto:
            params["mailto"] = self.mailto
        url = f"https://api.crossref.org/works?{urllib.parse.urlencode(params)}"
        payload = self._get_json(url)
        message = payload.get("message") if isinstance(payload, dict) else None
        items = message.get("items") if isinstance(message, dict) else None
        if not isinstance(items, list):
            return None
        best: MetadataCandidate | None = None
        for item in items:
            if not isinstance(item, dict):
                continue
            candidate_title = first_text(item.get("title"))
            if not candidate_title:
                continue
            candidate = crossref_work_to_candidate(item, score=title_match_score(title, candidate_title))
            if candidate is None:
                continue
            if best is None or candidate.score > best.score:
                best = candidate
        return best

    def _get_json(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": self.user_agent,
            },
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError(f"Expected JSON object from {url}")
        return payload


def crossref_work_to_candidate(work: dict[str, Any], *, score: float) -> MetadataCandidate | None:
    doi = normalize_doi(str(work.get("DOI") or ""))
    title = first_text(work.get("title"))
    if not title and not doi:
        return None
    fields = {
        "title": title,
        "DOI": doi,
        "url": str(work.get("URL") or ""),
        "date": crossref_date(work),
        "publicationTitle": first_text(work.get("container-title")),
        "ISSN": ", ".join(str(value) for value in work.get("ISSN") or [] if value),
        "volume": str(work.get("volume") or ""),
        "issue": str(work.get("issue") or ""),
        "pages": str(work.get("page") or ""),
        "journalAbbreviation": first_text(work.get("short-container-title")),
        "libraryCatalog": "Crossref",
        "abstractNote": strip_html(str(work.get("abstract") or "")),
    }
    locations = crossref_locations(work)
    return candidate_with_locations(
        source="crossref",
        identifier=doi or title,
        score=score,
        fields=fields,
        raw={
            "type": work.get("type"),
            "container_title": first_text(work.get("container-title")),
            "authors": work.get("author") or [],
            "links": work.get("link") or [],
        },
        locations=locations,
    )


def crossref_date(work: dict[str, Any]) -> str:
    for key in ("published-print", "published-online", "published", "issued", "created"):
        value = work.get(key)
        if not isinstance(value, dict):
            continue
        parts = value.get("date-parts")
        if not isinstance(parts, list) or not parts:
            continue
        first = parts[0]
        if not isinstance(first, list) or not first:
            continue
        year = str(first[0])
        month = f"-{int(first[1]):02d}" if len(first) > 1 and first[1] else ""
        day = f"-{int(first[2]):02d}" if len(first) > 2 and first[2] else ""
        return f"{year}{month}{day}"
    return ""


def crossref_locations(work: dict[str, Any]) -> list[FullTextLocation]:
    locations: list[FullTextLocation] = []
    seen: set[str] = set()
    for link in work.get("link") or []:
        if not isinstance(link, dict):
            continue
        url = first_text(link.get("URL") or link.get("url"))
        if not url or url in seen:
            continue
        seen.add(url)
        content_type = first_text(link.get("content-type"))
        locations.append(
            FullTextLocation(
                source="crossref",
                url=url,
                kind=crossref_link_kind(url, content_type),
                is_oa=None,
                version=first_text(link.get("content-version")),
                content_type=content_type,
                repository="Crossref",
                raw=link,
            )
        )
    landing = first_text(work.get("URL"))
    if landing and landing not in seen:
        locations.append(
            FullTextLocation(
                source="crossref",
                url=landing,
                kind="landing",
                is_oa=None,
                repository="Crossref",
                raw={"URL": landing},
            )
        )
    return locations


def crossref_link_kind(url: str, content_type: str = "") -> str:
    lowered_type = content_type.casefold()
    lowered_url = url.lower().split("?", 1)[0]
    if "pdf" in lowered_type or lowered_url.endswith(".pdf"):
        return "pdf"
    if "html" in lowered_type or lowered_url.endswith((".html", ".htm")):
        return "html"
    if "xml" in lowered_type or "jats" in lowered_type or lowered_url.endswith((".xml", ".jats")):
        return "xml"
    return "landing"
