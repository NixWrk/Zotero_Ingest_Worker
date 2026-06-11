from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from ..identifiers import normalize_arxiv_id, normalize_doi, normalize_pmid
from ..models import FullTextLocation, MetadataCandidate
from ..text import title_match_score
from .common import candidate_with_locations, first_text


SEMANTIC_SCHOLAR_FIELDS = (
    "title,abstract,year,venue,journal,publicationDate,url,openAccessPdf,"
    "externalIds,authors,citationCount,referenceCount"
)


class SemanticScholarClient:
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

    def by_doi(self, doi: str) -> MetadataCandidate | None:
        doi = normalize_doi(doi)
        return self._by_paper_id(f"DOI:{doi}") if doi else None

    def by_pmid(self, pmid: str) -> MetadataCandidate | None:
        pmid = normalize_pmid(pmid)
        return self._by_paper_id(f"PMID:{pmid}") if pmid else None

    def by_arxiv_id(self, arxiv_id: str) -> MetadataCandidate | None:
        arxiv_id = normalize_arxiv_id(arxiv_id)
        return self._by_paper_id(f"ARXIV:{arxiv_id}") if arxiv_id else None

    def by_title(self, title: str, *, rows: int = 5) -> MetadataCandidate | None:
        payload = self._get_json(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            {"query": title, "limit": str(rows), "fields": SEMANTIC_SCHOLAR_FIELDS},
        )
        rows_payload = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows_payload, list):
            return None
        best: MetadataCandidate | None = None
        for row in rows_payload:
            if not isinstance(row, dict):
                continue
            score = title_match_score(title, first_text(row.get("title")))
            candidate = semantic_scholar_paper_to_candidate(row, identifier=title, score=score)
            if candidate is not None and (best is None or candidate.score > best.score):
                best = candidate
        return best

    def _by_paper_id(self, paper_id: str) -> MetadataCandidate | None:
        payload = self._get_json(
            f"https://api.semanticscholar.org/graph/v1/paper/{urllib.parse.quote(paper_id, safe=':')}",
            {"fields": SEMANTIC_SCHOLAR_FIELDS},
        )
        return semantic_scholar_paper_to_candidate(payload, identifier=paper_id, score=1.0)

    def _get_json(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        headers = {"Accept": "application/json", "User-Agent": self.user_agent}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        request = urllib.request.Request(
            f"{url}?{urllib.parse.urlencode(params)}",
            headers=headers,
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError(f"Expected JSON object from {url}")
        return payload


def semantic_scholar_paper_to_candidate(paper: dict[str, Any], *, identifier: str, score: float) -> MetadataCandidate | None:
    title = first_text(paper.get("title"))
    external = paper.get("externalIds") if isinstance(paper.get("externalIds"), dict) else {}
    doi = normalize_doi(str(external.get("DOI") or ""))
    pmid = normalize_pmid(str(external.get("PubMed") or ""))
    if not title and not doi:
        return None
    journal = paper.get("journal") if isinstance(paper.get("journal"), dict) else {}
    fields = {
        "title": title,
        "abstractNote": first_text(paper.get("abstract")),
        "DOI": doi,
        "PMID": pmid,
        "date": first_text(paper.get("publicationDate") or paper.get("year")),
        "publicationTitle": first_text(journal.get("name") or paper.get("venue")),
        "volume": first_text(journal.get("volume")),
        "pages": first_text(journal.get("pages")),
        "url": first_text(paper.get("url")),
        "libraryCatalog": "Semantic Scholar",
    }
    locations: list[FullTextLocation] = []
    oa_pdf = paper.get("openAccessPdf") if isinstance(paper.get("openAccessPdf"), dict) else {}
    if oa_pdf.get("url"):
        locations.append(
            FullTextLocation(
                source="semantic_scholar",
                url=first_text(oa_pdf.get("url")),
                kind="pdf",
                is_oa=True,
                repository="Semantic Scholar",
                raw=oa_pdf,
            )
        )
    if paper.get("url"):
        locations.append(
            FullTextLocation(
                source="semantic_scholar",
                url=first_text(paper.get("url")),
                kind="landing",
                is_oa=None,
                repository="Semantic Scholar",
                raw={"paperId": paper.get("paperId")},
            )
        )
    return candidate_with_locations(
        source="semantic_scholar",
        identifier=identifier,
        score=score,
        fields=fields,
        raw={
            "paperId": paper.get("paperId"),
            "externalIds": external,
            "authors": paper.get("authors") or [],
            "citationCount": paper.get("citationCount"),
            "referenceCount": paper.get("referenceCount"),
        },
        locations=locations,
    )
