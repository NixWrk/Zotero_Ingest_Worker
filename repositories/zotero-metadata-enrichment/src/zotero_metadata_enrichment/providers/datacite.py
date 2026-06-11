from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from ..identifiers import normalize_doi
from ..models import FullTextLocation, MetadataCandidate
from .common import candidate_with_locations, first_text


class DataCiteClient:
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
        payload = self._get_json(f"https://api.datacite.org/dois/{urllib.parse.quote(doi, safe='')}")
        data = payload.get("data") if isinstance(payload, dict) else None
        return datacite_record_to_candidate(data) if isinstance(data, dict) else None

    def _get_json(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": self.user_agent},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError(f"Expected JSON object from {url}")
        return payload


def datacite_record_to_candidate(data: dict[str, Any]) -> MetadataCandidate | None:
    attrs = data.get("attributes") if isinstance(data.get("attributes"), dict) else {}
    doi = normalize_doi(str(attrs.get("doi") or data.get("id") or ""))
    title = first_text(attrs.get("titles"))
    if not doi and not title:
        return None
    descriptions = attrs.get("descriptions") if isinstance(attrs.get("descriptions"), list) else []
    abstract = ""
    for description in descriptions:
        if isinstance(description, dict) and str(description.get("descriptionType") or "").casefold() in {"abstract", "description"}:
            abstract = first_text(description.get("description"))
            if abstract:
                break
    fields = {
        "title": title,
        "abstractNote": abstract,
        "DOI": doi,
        "date": first_text(attrs.get("published") or attrs.get("publicationYear")),
        "publisher": first_text(attrs.get("publisher")),
        "url": first_text(attrs.get("url")),
        "libraryCatalog": "DataCite",
    }
    locations = datacite_locations(attrs)
    return candidate_with_locations(
        source="datacite",
        identifier=doi or title,
        score=1.0 if doi else 0.85,
        fields=fields,
        raw={"types": attrs.get("types"), "creators": attrs.get("creators") or [], "relationships": data.get("relationships")},
        locations=locations,
    )


def datacite_locations(attrs: dict[str, Any]) -> list[FullTextLocation]:
    locations: list[FullTextLocation] = []
    for url in attrs.get("contentUrl") or []:
        text = first_text(url)
        if text:
            locations.append(FullTextLocation(source="datacite", url=text, kind=guess_kind(text), is_oa=None, repository="DataCite", raw={"contentUrl": url}))
    landing = first_text(attrs.get("url"))
    if landing:
        locations.append(FullTextLocation(source="datacite", url=landing, kind="landing", is_oa=None, repository="DataCite", raw={"url": landing}))
    return locations


def guess_kind(url: str) -> str:
    lowered = url.lower().split("?", 1)[0]
    if lowered.endswith(".pdf"):
        return "pdf"
    if lowered.endswith((".xml", ".jats")):
        return "xml"
    if lowered.endswith((".html", ".htm")):
        return "html"
    return "landing"
