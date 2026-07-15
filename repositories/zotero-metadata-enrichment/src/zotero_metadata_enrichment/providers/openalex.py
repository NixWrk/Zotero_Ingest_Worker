from __future__ import annotations

import urllib.parse
import urllib.request
from typing import Any

from ..identifiers import normalize_doi, normalize_pmid
from ..models import FullTextLocation, MetadataCandidate
from ..provider_http import read_json_object
from ..text import title_match_score
from .common import as_dict, as_list, candidate_with_locations, compact_pages, first_text


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
        work = results[0]
        return (
            openalex_work_to_candidate(work, identifier=pmid, score=1.0)
            if isinstance(work, dict)
            else None
        )

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
        return read_json_object(request, timeout=self.timeout_seconds, error_label=url)


def openalex_work_to_candidate(work: dict[str, Any], *, identifier: str, score: float) -> MetadataCandidate | None:
    title = first_text(work.get("display_name"))
    ids = as_dict(work.get("ids"))
    doi = normalize_doi(str(ids.get("doi") or work.get("doi") or ""))
    if not title and not doi:
        return None
    biblio = as_dict(work.get("biblio"))
    source = openalex_source(work)
    open_access = as_dict(work.get("open_access"))
    fields = {
        "title": title,
        "abstractNote": openalex_abstract(work.get("abstract_inverted_index")),
        "DOI": doi,
        "date": first_text(work.get("publication_date") or work.get("publication_year")),
        "publicationTitle": first_text(source.get("display_name")),
        "ISSN": ", ".join(str(value) for value in as_list(source.get("issn")) if value),
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
    primary = as_dict(work.get("primary_location"))
    source = as_dict(primary.get("source"))
    if source:
        return source
    for location in as_list(work.get("locations")):
        if isinstance(location, dict):
            source = as_dict(location.get("source"))
            if source:
                return source
    return {}


def openalex_locations(work: dict[str, Any]) -> list[FullTextLocation]:
    raw_locations = []
    primary = work.get("primary_location")
    if isinstance(primary, dict):
        raw_locations.append(primary)
    raw_locations.extend(loc for loc in as_list(work.get("locations")) if isinstance(loc, dict))
    locations: list[FullTextLocation] = []
    seen: set[str] = set()
    for raw in raw_locations:
        for key, kind in (("pdf_url", "pdf"), ("landing_page_url", "landing")):
            url = first_text(raw.get(key))
            if not url or url in seen:
                continue
            seen.add(url)
            source = as_dict(raw.get("source"))
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
