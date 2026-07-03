from __future__ import annotations

import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from ..identifiers import extract_arxiv_id_from_text, normalize_arxiv_id
from ..models import FullTextLocation, MetadataCandidate
from ..provider_http import read_text
from ..text import normalize_space, title_match_score
from .common import candidate_with_locations

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"


class ArxivClient:
    def __init__(
        self,
        *,
        timeout_seconds: int = 60,
        user_agent: str = "zotero-metadata-enrichment/0.1",
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent

    def by_id(self, arxiv_id: str) -> MetadataCandidate | None:
        arxiv_id = normalize_arxiv_id(arxiv_id)
        if not arxiv_id:
            return None
        params = {"id_list": arxiv_id, "max_results": "1"}
        candidates = parse_arxiv_atom(self._get_text(params))
        if not candidates:
            return None
        candidate = candidates[0]
        return MetadataCandidate(
            source=candidate.source,
            identifier=normalize_arxiv_id(candidate.identifier) or candidate.identifier,
            score=1.0,
            fields=candidate.fields,
            raw={**candidate.raw, "match": "identifier"},
        )

    def by_title(self, title: str, *, rows: int = 5) -> MetadataCandidate | None:
        params = {
            "search_query": f'ti:"{title}"',
            "start": "0",
            "max_results": str(rows),
        }
        candidates = parse_arxiv_atom(self._get_text(params))
        best: MetadataCandidate | None = None
        for candidate in candidates:
            scored = MetadataCandidate(
                source=candidate.source,
                identifier=candidate.identifier,
                score=title_match_score(title, candidate.fields.get("title", "")),
                fields=candidate.fields,
                raw={**candidate.raw, "match": "title"},
            )
            if best is None or scored.score > best.score:
                best = scored
        return best

    def _get_text(self, params: dict[str, str]) -> str:
        url = f"https://arxiv.org/api/query?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.8",
                "User-Agent": self.user_agent,
            },
            method="GET",
        )
        return read_text(request, timeout=self.timeout_seconds)


def parse_arxiv_atom(xml_text: str) -> list[MetadataCandidate]:
    root = ET.fromstring(xml_text)
    candidates: list[MetadataCandidate] = []
    for entry in root.findall(f"{ATOM}entry"):
        arxiv_url = entry_text(entry, "id")
        arxiv_id = extract_arxiv_id_from_text(arxiv_url or "") or ""
        title = normalize_space(entry_text(entry, "title"))
        summary = normalize_space(entry_text(entry, "summary"))
        updated = date_part(entry_text(entry, "updated") or entry_text(entry, "published"))
        doi = entry_text(entry, "doi", namespace=ARXIV)
        primary_category = ""
        primary = entry.find(f"{ARXIV}primary_category")
        if primary is not None:
            primary_category = str(primary.attrib.get("term") or "")
        if not primary_category:
            category = entry.find(f"{ATOM}category")
            if category is not None:
                primary_category = str(category.attrib.get("term") or "")

        fields = {
            "title": title,
            "abstractNote": summary,
            "date": updated,
            "url": f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else arxiv_url,
            "archive": "arXiv",
            "archiveLocation": arxiv_id,
            "libraryCatalog": "arXiv.org",
        }
        if doi:
            fields["DOI"] = doi
        elif arxiv_id:
            fields["DOI"] = f"10.48550/arXiv.{arxiv_id}"
        if arxiv_id:
            fields["extra"] = (
                f"arXiv:{arxiv_id} [{primary_category}]"
                if primary_category
                else f"arXiv:{arxiv_id}"
            )
        locations = []
        if arxiv_id:
            locations.extend(
                [
                    FullTextLocation(
                        source="arxiv",
                        url=f"https://arxiv.org/html/{arxiv_id}",
                        kind="html",
                        is_oa=True,
                        repository="arXiv",
                        raw={"arxiv_id": arxiv_id},
                    ),
                    FullTextLocation(
                        source="ar5iv",
                        url=f"https://ar5iv.labs.arxiv.org/html/{arxiv_id}",
                        kind="html",
                        is_oa=True,
                        repository="ar5iv",
                        raw={"arxiv_id": arxiv_id, "fallback_for": "arxiv_html"},
                    ),
                    FullTextLocation(
                        source="arxiv",
                        url=f"https://arxiv.org/pdf/{arxiv_id}",
                        kind="pdf",
                        is_oa=True,
                        repository="arXiv",
                        raw={"arxiv_id": arxiv_id},
                    ),
                ]
            )
        candidates.append(
            candidate_with_locations(
                source="arxiv",
                identifier=arxiv_id,
                score=1.0,
                fields={key: value for key, value in fields.items() if value},
                raw={
                    "id": arxiv_url,
                    "arxiv_id": arxiv_id,
                    "authors": [normalize_space(entry_text(author, "name")) for author in entry.findall(f"{ATOM}author")],
                    "primary_category": primary_category,
                },
                locations=locations,
            )
        )
    return candidates


def entry_text(entry: ET.Element, name: str, *, namespace: str = ATOM) -> str:
    return str(entry.findtext(f"{namespace}{name}") or "")


def date_part(value: str) -> str:
    value = str(value or "").strip()
    return value[:10] if value else ""
