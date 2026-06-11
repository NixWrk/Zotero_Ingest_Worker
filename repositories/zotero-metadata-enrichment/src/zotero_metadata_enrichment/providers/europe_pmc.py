from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from ..identifiers import normalize_doi, normalize_pmcid, normalize_pmid
from ..models import FullTextLocation, MetadataCandidate
from .common import candidate_with_locations, first_text


class EuropePmcClient:
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
        return self._search_one(f'DOI:"{doi}"')

    def by_pmid(self, pmid: str) -> MetadataCandidate | None:
        pmid = normalize_pmid(pmid)
        if not pmid:
            return None
        return self._search_one(f"EXT_ID:{pmid} AND SRC:MED")

    def by_pmcid(self, pmcid: str) -> MetadataCandidate | None:
        pmcid = normalize_pmcid(pmcid)
        if not pmcid:
            return None
        return self._search_one(f"PMCID:{pmcid}")

    def _search_one(self, query: str) -> MetadataCandidate | None:
        payload = self._get_json(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            {"query": query, "format": "json", "pageSize": "1", "resultType": "core"},
        )
        result_list = payload.get("resultList") if isinstance(payload, dict) else None
        results = result_list.get("result") if isinstance(result_list, dict) else None
        if not isinstance(results, list) or not results:
            return None
        return europe_pmc_result_to_candidate(results[0])

    def _get_json(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{url}?{urllib.parse.urlencode(params)}",
            headers={"Accept": "application/json", "User-Agent": self.user_agent},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError(f"Expected JSON object from {url}")
        return payload


def europe_pmc_result_to_candidate(result: dict[str, Any]) -> MetadataCandidate | None:
    title = first_text(result.get("title"))
    doi = normalize_doi(str(result.get("doi") or ""))
    pmid = normalize_pmid(str(result.get("pmid") or ""))
    pmcid = normalize_pmcid(str(result.get("pmcid") or ""))
    if not title and not doi and not pmid and not pmcid:
        return None
    fields = {
        "title": title,
        "abstractNote": first_text(result.get("abstractText")),
        "DOI": doi,
        "PMID": pmid,
        "PMCID": pmcid,
        "date": first_text(result.get("firstPublicationDate") or result.get("pubYear")),
        "publicationTitle": first_text(result.get("journalTitle")),
        "journalAbbreviation": first_text(result.get("journalAbbreviation")),
        "ISSN": first_text(result.get("journalIssn")),
        "volume": first_text(result.get("journalVolume")),
        "issue": first_text(result.get("issue")),
        "pages": first_text(result.get("pageInfo")),
        "url": europe_pmc_url(result, pmid=pmid, pmcid=pmcid),
        "libraryCatalog": "Europe PMC",
    }
    locations = europe_pmc_locations(result, pmid=pmid, pmcid=pmcid)
    return candidate_with_locations(
        source="europe_pmc",
        identifier=pmcid or pmid or doi or title,
        score=1.0,
        fields=fields,
        raw={"source": result.get("source"), "isOpenAccess": result.get("isOpenAccess"), "hasFullText": result.get("hasFullText")},
        locations=locations,
    )


def europe_pmc_url(result: dict[str, Any], *, pmid: str, pmcid: str) -> str:
    if pmcid:
        return f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
    if pmid:
        return f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    return first_text(result.get("id"))


def europe_pmc_locations(result: dict[str, Any], *, pmid: str, pmcid: str) -> list[FullTextLocation]:
    locations: list[FullTextLocation] = []
    if pmcid:
        locations.append(
            FullTextLocation(
                source="europe_pmc",
                url=f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/",
                kind="html",
                is_oa=str(result.get("isOpenAccess") or "").upper() == "Y",
                repository="PMC",
                raw={"pmcid": pmcid},
            )
        )
        numeric = pmcid.removeprefix("PMC")
        locations.append(
            FullTextLocation(
                source="pmc_oai",
                url=f"https://pmc.ncbi.nlm.nih.gov/api/oai/v1/mh/?verb=GetRecord&identifier=oai:pubmedcentral.nih.gov:{numeric}&metadataPrefix=pmc",
                kind="xml",
                is_oa=str(result.get("isOpenAccess") or "").upper() == "Y",
                content_type="application/xml",
                repository="PMC Open Access",
                raw={"pmcid": pmcid, "oai_identifier": f"oai:pubmedcentral.nih.gov:{numeric}"},
            )
        )
    if pmid:
        locations.append(
            FullTextLocation(
                source="europe_pmc",
                url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                kind="landing",
                is_oa=None,
                repository="PubMed",
                raw={"pmid": pmid},
            )
        )
    full_text_list = result.get("fullTextUrlList")
    urls = full_text_list.get("fullTextUrl") if isinstance(full_text_list, dict) else None
    if isinstance(urls, list):
        for item in urls:
            if not isinstance(item, dict):
                continue
            url = first_text(item.get("url"))
            if not url:
                continue
            locations.append(
                FullTextLocation(
                    source="europe_pmc",
                    url=url,
                    kind=first_text(item.get("documentStyle")).lower() or "landing",
                    is_oa=True,
                    content_type=first_text(item.get("availability")),
                    repository=first_text(item.get("site")),
                    raw=item,
                )
            )
    return locations
