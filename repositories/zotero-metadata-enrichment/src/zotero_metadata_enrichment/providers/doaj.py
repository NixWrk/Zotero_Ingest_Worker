from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from ..identifiers import normalize_doi
from ..models import FullTextLocation, MetadataCandidate
from ..text import title_match_score
from .common import candidate_with_locations, first_text


class DoajClient:
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
        payload = self._get_json(f"https://doaj.org/api/search/articles/{urllib.parse.quote('doi:' + doi, safe='')}")
        return doaj_payload_to_candidate(payload, identifier=doi, expected_title="")

    def by_title(self, title: str) -> MetadataCandidate | None:
        if not title:
            return None
        payload = self._get_json(f"https://doaj.org/api/search/articles/{urllib.parse.quote('title:' + title, safe='')}")
        return doaj_payload_to_candidate(payload, identifier=title, expected_title=title)

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


def doaj_payload_to_candidate(payload: dict[str, Any], *, identifier: str, expected_title: str) -> MetadataCandidate | None:
    results = payload.get("results") if isinstance(payload.get("results"), list) else []
    if not results:
        return None
    bibjson = results[0].get("bibjson") if isinstance(results[0], dict) else None
    if not isinstance(bibjson, dict):
        return None
    score = title_match_score(expected_title, first_text(bibjson.get("title"))) if expected_title else 1.0
    return doaj_bibjson_to_candidate(bibjson, identifier=identifier, score=score)


def doaj_bibjson_to_candidate(bibjson: dict[str, Any], *, identifier: str, score: float) -> MetadataCandidate | None:
    title = first_text(bibjson.get("title"))
    doi = normalize_doi(doaj_identifier(bibjson, "doi"))
    if not title and not doi:
        return None
    journal = bibjson.get("journal") if isinstance(bibjson.get("journal"), dict) else {}
    fields = {
        "title": title,
        "abstractNote": first_text(bibjson.get("abstract")),
        "DOI": doi,
        "date": first_text(bibjson.get("year") or bibjson.get("month")),
        "publicationTitle": first_text(journal.get("title")),
        "ISSN": ", ".join(str(value) for value in journal.get("issns") or [] if value),
        "publisher": first_text(journal.get("publisher")),
        "url": doaj_best_url(bibjson),
        "libraryCatalog": "DOAJ",
    }
    locations = doaj_locations(bibjson)
    return candidate_with_locations(
        source="doaj",
        identifier=identifier,
        score=score,
        fields=fields,
        raw={"journal": journal, "keywords": bibjson.get("keywords") or []},
        locations=locations,
    )


def doaj_identifier(bibjson: dict[str, Any], kind: str) -> str:
    for identifier in bibjson.get("identifier") or []:
        if isinstance(identifier, dict) and str(identifier.get("type") or "").casefold() == kind.casefold():
            return first_text(identifier.get("id"))
    return ""


def doaj_best_url(bibjson: dict[str, Any]) -> str:
    locations = doaj_locations(bibjson)
    return locations[0].url if locations else ""


def doaj_locations(bibjson: dict[str, Any]) -> list[FullTextLocation]:
    links = bibjson.get("link") if isinstance(bibjson.get("link"), list) else []
    locations: list[FullTextLocation] = []
    for link in links:
        if not isinstance(link, dict):
            continue
        url = first_text(link.get("url"))
        if not url:
            continue
        link_type = first_text(link.get("type")).casefold()
        locations.append(
            FullTextLocation(
                source="doaj",
                url=url,
                kind="pdf" if "pdf" in link_type or url.lower().split("?", 1)[0].endswith(".pdf") else "landing",
                is_oa=True,
                repository="DOAJ",
                raw=link,
            )
        )
    return locations
