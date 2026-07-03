from __future__ import annotations

import urllib.parse
import urllib.request
from typing import Any

from ..identifiers import normalize_doi
from ..models import FullTextLocation, MetadataCandidate
from ..provider_http import read_json_object
from .common import candidate_with_locations, first_text


class OpenAireClient:
    def __init__(
        self,
        *,
        timeout_seconds: int = 60,
        user_agent: str = "zotero-metadata-enrichment/0.1",
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent

    def by_doi(self, doi: str) -> MetadataCandidate | None:
        doi = normalize_doi(doi)
        if not doi:
            return None
        payload = self._get_json("https://api.openaire.eu/search/researchProducts", {"doi": doi, "format": "json", "size": "1"})
        return openaire_payload_to_candidate(payload, identifier=doi)

    def _get_json(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{url}?{urllib.parse.urlencode(params)}",
            headers={"Accept": "application/json", "User-Agent": self.user_agent},
            method="GET",
        )
        return read_json_object(request, timeout=self.timeout_seconds, error_label=url)


def openaire_payload_to_candidate(payload: dict[str, Any], *, identifier: str) -> MetadataCandidate | None:
    results = payload.get("response", {}).get("results", {}).get("result")
    if isinstance(results, dict):
        result = results
    elif isinstance(results, list) and results:
        result = results[0]
    else:
        return None
    metadata = result.get("metadata", {}).get("oaf:entity", {}).get("oaf:result", {})
    if not isinstance(metadata, dict):
        return None
    title = openaire_text(metadata.get("title"))
    doi = normalize_doi(openaire_identifier(metadata.get("pid"), "doi") or identifier)
    if not title and not doi:
        return None
    fields = {
        "title": title,
        "abstractNote": openaire_text(metadata.get("description")),
        "DOI": doi,
        "date": openaire_text(metadata.get("dateofacceptance")),
        "publicationTitle": openaire_text(metadata.get("journal")),
        "publisher": openaire_text(metadata.get("publisher")),
        "url": openaire_best_url(metadata),
        "libraryCatalog": "OpenAIRE",
    }
    locations = openaire_locations(metadata)
    return candidate_with_locations(
        source="openaire",
        identifier=identifier,
        score=1.0,
        fields=fields,
        raw={"result": result},
        locations=locations,
    )


def openaire_text(value: Any) -> str:
    if isinstance(value, dict):
        return first_text(value.get("$") or value.get("value"))
    if isinstance(value, list):
        return first_text(value[0]) if value else ""
    return first_text(value)


def openaire_identifier(value: Any, scheme: str) -> str:
    entries = value if isinstance(value, list) else [value]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        qualifier = entry.get("@classid") or entry.get("@schemeid") or entry.get("@schemename")
        if str(qualifier or "").casefold() == scheme.casefold():
            return openaire_text(entry)
    return ""


def openaire_best_url(metadata: dict[str, Any]) -> str:
    locations = openaire_locations(metadata)
    return locations[0].url if locations else ""


def openaire_locations(metadata: dict[str, Any]) -> list[FullTextLocation]:
    locations: list[FullTextLocation] = []
    values = metadata.get("children", {}).get("instance") if isinstance(metadata.get("children"), dict) else None
    entries = values if isinstance(values, list) else [values]
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        url = openaire_text(entry.get("url"))
        if not url or url in seen:
            continue
        seen.add(url)
        locations.append(FullTextLocation(source="openaire", url=url, kind="landing", is_oa=True, repository=openaire_text(entry.get("hostedby")), raw=entry))
    return locations
