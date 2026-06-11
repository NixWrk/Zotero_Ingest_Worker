from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from ..identifiers import normalize_doi
from ..models import FullTextLocation, MetadataCandidate
from .common import candidate_with_locations, first_text


class BioRxivClient:
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
        for server in ("biorxiv", "medrxiv"):
            candidate = self._by_doi_on_server(server, doi)
            if candidate is not None:
                return candidate
        return None

    def _by_doi_on_server(self, server: str, doi: str) -> MetadataCandidate | None:
        payload = self._get_json(f"https://api.biorxiv.org/details/{server}/{urllib.parse.quote(doi, safe='/')}/na/json")
        collection = payload.get("collection") if isinstance(payload, dict) else None
        if not isinstance(collection, list) or not collection:
            return None
        return biorxiv_record_to_candidate(collection[0], server=server)

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


def biorxiv_record_to_candidate(record: dict[str, Any], *, server: str) -> MetadataCandidate | None:
    doi = normalize_doi(str(record.get("doi") or ""))
    title = first_text(record.get("title"))
    if not doi and not title:
        return None
    landing = first_text(record.get("url"))
    if not landing and doi:
        landing = f"https://www.{server}.org/content/{doi}"
    fields = {
        "title": title,
        "abstractNote": first_text(record.get("abstract")),
        "DOI": doi,
        "date": first_text(record.get("date")),
        "publicationTitle": "bioRxiv" if server == "biorxiv" else "medRxiv",
        "repository": "bioRxiv" if server == "biorxiv" else "medRxiv",
        "url": landing,
        "libraryCatalog": server,
        "extra": f"Published DOI: {normalize_doi(str(record.get('published') or ''))}" if record.get("published") else "",
    }
    locations = []
    if landing:
        locations.append(FullTextLocation(source=server, url=landing, kind="html", is_oa=True, repository=server, raw=record))
    if doi:
        locations.append(
            FullTextLocation(
                source=server,
                url=f"https://www.{server}.org/content/{doi}.full.pdf",
                kind="pdf",
                is_oa=True,
                repository=server,
                raw=record,
            )
        )
    return candidate_with_locations(
        source=server,
        identifier=doi or title,
        score=1.0 if doi else 0.85,
        fields=fields,
        raw={"authors": record.get("authors"), "category": record.get("category"), "version": record.get("version")},
        locations=locations,
    )
