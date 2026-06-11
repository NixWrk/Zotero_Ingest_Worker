from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from ..identifiers import normalize_doi, normalize_pmid
from ..models import FullTextLocation, MetadataCandidate
from ..text import title_match_score
from .common import candidate_with_locations, compact_pages, first_text


class OpenAlexClient:
    def __init__(
        self,
        *,
        mailto: str = "",
        api_key: str = "",
        timeout_seconds: int = 60,
        user_agent: str = "zotero-metadata-enrichment/0.1",
    ) -> None:
        self.mailto = mailto.strip()
        self.api_key = api_key.strip()
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent

    def by_doi(self, doi: str) -> MetadataCandidate | None:
        doi = normalize_doi(doi)
        if not doi:
            return None
        url = f"https://api.openalex.org/works/https://doi.org/{urllib.parse.quote(doi, safe='/')}"
        payload = self._get_json(url, self._polite_params())
        return openalex_work_to_candidate(payload, identifier=doi, score=1.0)

    def by_pmid(self, pmid: str) -> MetadataCandidate | None:
        pmid = normalize_pmid(pmid)
        if not pmid:
            return None
        payload = self._get_json("https://api.openalex.org/works", {**self._polite_params(), "filter": f"ids.pmid:{pmid}", "per-page": "1"})
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list) or not results:
            return None
        return openalex_work_to_candidate(results[0], identifier=pmid, score=1.0)

    def by_title(self, title: str, *, rows: int = 5) -> MetadataCandidate | None:
        payload = self._get_json(
            "https://api.openalex.org/works",
            {**self._polite_params(), "search": title, "per-page": str(rows)},
        )
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            return None
        best: MetadataCandidate | None = None
        for work in results:
            if not isinstance(work, dict):
                continue
            score = title_match_score(title, first_text(work.get("display_name")))
            candidate = openalex_work_to_candidate(work, identifier=title, score=score)
            if candidate is not None and (best is None or candidate.score > best.score):
                best = candidate
        return best

    def _polite_params(self) -> dict[str, str]:
        params: dict[str, str] = {}
        if self.mailto:
            params["mailto"] = self.mailto
        if self.api_key:
            params["api_key"] = self.api_key
        return params

    def _get_json(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        request = urllib.request.Request(
            f"{url}{query}",
            headers={"Accept": "application/json", "User-Agent": self.user_agent},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError(f"Expected JSON object from {url}")
        return payload


def openalex_work_to_candidate(work: dict[str, Any], *, identifier: str, score: float) -> MetadataCandidate | None:
    title = first_text(work.get("display_name"))
    ids = work.get("ids") if isinstance(work.get("ids"), dict) else {}
    doi = normalize_doi(str(ids.get("doi") or work.get("doi") or ""))
    if not title and not doi:
        return None
    biblio = work.get("biblio") if isinstance(work.get("biblio"), dict) else {}
    source = openalex_source(work)
    open_access = work.get("open_access") if isinstance(work.get("open_access"), dict) else {}
    fields = {
        "title": title,
        "abstractNote": openalex_abstract(work.get("abstract_inverted_index")),
        "DOI": doi,
        "date": first_text(work.get("publication_date") or work.get("publication_year")),
        "publicationTitle": first_text(source.get("display_name")),
        "ISSN": ", ".join(str(value) for value in source.get("issn") or [] if value),
        "volume": first_text(biblio.get("volume")),
        "issue": first_text(biblio.get("issue")),
        "pages": compact_pages(first_text(biblio.get("first_page")), first_text(biblio.get("last_page"))),
        "url": first_text(work.get("primary_location", {}).get("landing_page_url") if isinstance(work.get("primary_location"), dict) else work.get("id")),
        "libraryCatalog": "OpenAlex",
    }
    locations = openalex_locations(work)
    return candidate_with_locations(
        source="openalex",
        identifier=identifier,
        score=score,
        fields=fields,
        raw={
            "id": work.get("id"),
            "ids": ids,
            "open_access": open_access,
            "type": work.get("type"),
            "authorships": work.get("authorships") or [],
            "topics": work.get("topics") or [],
        },
        locations=locations,
    )


def openalex_source(work: dict[str, Any]) -> dict[str, Any]:
    primary = work.get("primary_location") if isinstance(work.get("primary_location"), dict) else {}
    source = primary.get("source") if isinstance(primary.get("source"), dict) else {}
    if source:
        return source
    for location in work.get("locations") or []:
        if isinstance(location, dict) and isinstance(location.get("source"), dict):
            return location["source"]
    return {}


def openalex_locations(work: dict[str, Any]) -> list[FullTextLocation]:
    raw_locations = []
    primary = work.get("primary_location")
    if isinstance(primary, dict):
        raw_locations.append(primary)
    raw_locations.extend(loc for loc in work.get("locations") or [] if isinstance(loc, dict))
    locations: list[FullTextLocation] = []
    seen: set[str] = set()
    for raw in raw_locations:
        for key, kind in (("pdf_url", "pdf"), ("landing_page_url", "landing")):
            url = first_text(raw.get(key))
            if not url or url in seen:
                continue
            seen.add(url)
            source = raw.get("source") if isinstance(raw.get("source"), dict) else {}
            locations.append(
                FullTextLocation(
                    source="openalex",
                    url=url,
                    kind=kind,
                    is_oa=bool(raw.get("is_oa")) if raw.get("is_oa") is not None else None,
                    license=first_text(raw.get("license")),
                    version=first_text(raw.get("version")),
                    repository=first_text(source.get("display_name")),
                    raw=raw,
                )
            )
    return locations


def openalex_abstract(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    positions: list[tuple[int, str]] = []
    for word, indexes in value.items():
        if not isinstance(indexes, list):
            continue
        for index in indexes:
            try:
                positions.append((int(index), str(word)))
            except (TypeError, ValueError):
                continue
    return " ".join(word for _, word in sorted(positions))
