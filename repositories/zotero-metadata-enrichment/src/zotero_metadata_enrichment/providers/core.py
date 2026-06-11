from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from ..identifiers import normalize_doi
from ..models import FullTextLocation, MetadataCandidate
from ..text import title_match_score
from .common import candidate_with_locations, first_text


class CoreClient:
    def __init__(
        self,
        *,
        api_key: str = "",
        timeout_seconds: int = 60,
        user_agent: str = "zotero-metadata-enrichment/0.1",
    ) -> None:
        self.api_key = api_key.strip()
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def by_doi(self, doi: str) -> MetadataCandidate | None:
        doi = normalize_doi(doi)
        if not doi or not self.enabled:
            return None
        return self._search_one(f'doi:"{doi}"', identifier=doi, expected_title="")

    def by_title(self, title: str, *, rows: int = 5) -> MetadataCandidate | None:
        if not title or not self.enabled:
            return None
        payload = self._search(f'title:"{title}"', limit=rows)
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            return None
        best: MetadataCandidate | None = None
        for result in results:
            if not isinstance(result, dict):
                continue
            score = title_match_score(title, first_text(result.get("title")))
            candidate = core_work_to_candidate(result, identifier=title, score=score)
            if candidate is not None and (best is None or candidate.score > best.score):
                best = candidate
        return best

    def _search_one(self, query: str, *, identifier: str, expected_title: str) -> MetadataCandidate | None:
        payload = self._search(query, limit=1)
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list) or not results:
            return None
        score = title_match_score(expected_title, first_text(results[0].get("title"))) if expected_title else 1.0
        return core_work_to_candidate(results[0], identifier=identifier, score=score)

    def _search(self, query: str, *, limit: int) -> dict[str, Any]:
        request = urllib.request.Request(
            f"https://api.core.ac.uk/v3/search/works?{urllib.parse.urlencode({'q': query, 'limit': str(limit)})}",
            headers={
                "Accept": "application/json",
                "User-Agent": self.user_agent,
                "Authorization": f"Bearer {self.api_key}",
            },
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("Expected JSON object from CORE")
        return payload


def core_work_to_candidate(work: dict[str, Any], *, identifier: str, score: float) -> MetadataCandidate | None:
    title = first_text(work.get("title"))
    doi = normalize_doi(first_text(work.get("doi")))
    if not title and not doi:
        return None
    fields = {
        "title": title,
        "abstractNote": first_text(work.get("abstract")),
        "DOI": doi,
        "date": first_text(work.get("publishedDate") or work.get("yearPublished")),
        "publicationTitle": first_text(work.get("journals")),
        "url": first_text(work.get("downloadUrl") or work.get("fullTextLink") or work.get("sourceFulltextUrls")),
        "libraryCatalog": "CORE",
    }
    locations = core_locations(work)
    return candidate_with_locations(
        source="core",
        identifier=identifier,
        score=score,
        fields=fields,
        raw={"id": work.get("id"), "repository": work.get("repository"), "authors": work.get("authors") or []},
        locations=locations,
    )


def core_locations(work: dict[str, Any]) -> list[FullTextLocation]:
    locations: list[FullTextLocation] = []
    values: list[Any] = []
    for key in ("downloadUrl", "fullTextLink", "sourceFulltextUrls"):
        value = work.get(key)
        if isinstance(value, list):
            values.extend(value)
        elif value:
            values.append(value)
    seen: set[str] = set()
    for value in values:
        url = first_text(value)
        if not url or url in seen:
            continue
        seen.add(url)
        locations.append(FullTextLocation(source="core", url=url, kind="pdf" if url.lower().split("?", 1)[0].endswith(".pdf") else "landing", is_oa=True, repository="CORE", raw={"url": url}))
    return locations
