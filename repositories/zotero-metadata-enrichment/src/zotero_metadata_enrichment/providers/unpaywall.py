from __future__ import annotations

import urllib.parse
import urllib.request
from typing import Any

from ..identifiers import normalize_doi
from ..models import FullTextLocation, MetadataCandidate
from ..provider_http import read_json_object
from .common import as_dict, as_list, candidate_with_locations, first_text


class UnpaywallClient:
    def __init__(
        self,
        *,
        email: str = "",
        timeout_seconds: int = 60,
        user_agent: str = "zotero-metadata-enrichment/0.1",
    ) -> None:
        self.email = email.strip()
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent

    @property
    def enabled(self) -> bool:
        return bool(self.email)

    def by_doi(self, doi: str) -> MetadataCandidate | None:
        doi = normalize_doi(doi)
        if not doi or not self.enabled:
            return None
        payload = self._get_json(
            f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi, safe='')}",
            {"email": self.email},
        )
        return unpaywall_item_to_candidate(payload)

    def _get_json(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{url}?{urllib.parse.urlencode(params)}",
            headers={"Accept": "application/json", "User-Agent": self.user_agent},
            method="GET",
        )
        return read_json_object(request, timeout=self.timeout_seconds, error_label=url)


def unpaywall_item_to_candidate(item: dict[str, Any]) -> MetadataCandidate | None:
    doi = normalize_doi(str(item.get("doi") or ""))
    title = first_text(item.get("title"))
    if not doi and not title:
        return None
    best = as_dict(item.get("best_oa_location"))
    locations = unpaywall_locations(item)
    fields = {
        "title": title,
        "DOI": doi,
        "date": str(item.get("year") or ""),
        "publicationTitle": first_text(item.get("journal_name")),
        "ISSN": first_text(item.get("journal_issns")),
        "publisher": first_text(item.get("publisher")),
        "url": first_text(best.get("url_for_landing_page") or best.get("url") or item.get("doi_url")),
        "libraryCatalog": "Unpaywall",
    }
    return candidate_with_locations(
        source="unpaywall",
        identifier=doi or title,
        score=1.0 if doi else 0.85,
        fields=fields,
        raw={
            "is_oa": item.get("is_oa"),
            "oa_status": item.get("oa_status"),
            "genre": item.get("genre"),
            "best_oa_location": best,
        },
        locations=locations,
    )


def unpaywall_locations(item: dict[str, Any]) -> list[FullTextLocation]:
    raw_locations = as_list(item.get("oa_locations"))
    best = item.get("best_oa_location")
    if isinstance(best, dict):
        raw_locations = [best, *[loc for loc in raw_locations if loc is not best]]
    locations: list[FullTextLocation] = []
    seen: set[str] = set()
    for raw in raw_locations:
        if not isinstance(raw, dict):
            continue
        for key, kind in (("url_for_pdf", "pdf"), ("url_for_landing_page", "landing"), ("url", "landing")):
            url = first_text(raw.get(key))
            if not url or url in seen:
                continue
            seen.add(url)
            locations.append(
                FullTextLocation(
                    source="unpaywall",
                    url=url,
                    kind=kind,
                    is_oa=True,
                    license=first_text(raw.get("license")),
                    version=first_text(raw.get("version")),
                    repository=first_text(raw.get("repository_institution") or raw.get("host_type")),
                    raw=raw,
                )
            )
    return locations
