from __future__ import annotations

import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .identifiers import extract_arxiv_id_from_text, normalize_arxiv_id
from .models import ArxivCandidate
from .text import normalize_space, title_match_score

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"


class ArxivLookupClient:
    def __init__(
        self,
        *,
        timeout_seconds: int = 60,
        user_agent: str = "zotero-arxiv-html-ingest/0.1",
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent

    def by_id(self, arxiv_id: str) -> ArxivCandidate | None:
        arxiv_id = normalize_arxiv_id(arxiv_id)
        if not arxiv_id:
            return None
        candidates = parse_arxiv_atom(
            self._get_text({"id_list": arxiv_id, "max_results": "1"})
        )
        if not candidates:
            return None
        candidate = candidates[0]
        return ArxivCandidate(
            arxiv_id=normalize_arxiv_id(candidate.arxiv_id) or candidate.arxiv_id,
            score=1.0,
            title=candidate.title,
            abstract=candidate.abstract,
            url=candidate.url,
            doi=candidate.doi,
            source=candidate.source,
            raw={**candidate.raw, "match": "identifier"},
        )

    def by_title(self, title: str, *, rows: int = 5) -> ArxivCandidate | None:
        candidates = parse_arxiv_atom(
            self._get_text(
                {
                    "search_query": f'ti:"{title}"',
                    "start": "0",
                    "max_results": str(rows),
                }
            )
        )
        best: ArxivCandidate | None = None
        for candidate in candidates:
            scored = ArxivCandidate(
                arxiv_id=candidate.arxiv_id,
                score=title_match_score(title, candidate.title),
                title=candidate.title,
                abstract=candidate.abstract,
                url=candidate.url,
                doi=candidate.doi,
                source=candidate.source,
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
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")


def parse_arxiv_atom(xml_text: str) -> list[ArxivCandidate]:
    root = ET.fromstring(xml_text)
    candidates: list[ArxivCandidate] = []
    for entry in root.findall(f"{ATOM}entry"):
        arxiv_url = entry_text(entry, "id")
        arxiv_id = extract_arxiv_id_from_text(arxiv_url or "") or ""
        title = normalize_space(entry_text(entry, "title"))
        summary = normalize_space(entry_text(entry, "summary"))
        doi = entry_text(entry, "doi", namespace=ARXIV)
        primary_category = ""
        primary = entry.find(f"{ARXIV}primary_category")
        if primary is not None:
            primary_category = str(primary.attrib.get("term") or "")
        if not primary_category:
            category = entry.find(f"{ATOM}category")
            if category is not None:
                primary_category = str(category.attrib.get("term") or "")
        candidates.append(
            ArxivCandidate(
                arxiv_id=arxiv_id,
                score=1.0,
                title=title,
                abstract=summary,
                url=f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else arxiv_url,
                doi=doi or (f"10.48550/arXiv.{arxiv_id}" if arxiv_id else ""),
                source="arxiv",
                raw={
                    "id": arxiv_url,
                    "authors": [normalize_space(entry_text(author, "name")) for author in entry.findall(f"{ATOM}author")],
                    "primary_category": primary_category,
                    "published": entry_text(entry, "published"),
                    "updated": entry_text(entry, "updated"),
                },
            )
        )
    return candidates


def entry_text(entry: ET.Element, name: str, *, namespace: str = ATOM) -> str:
    return str(entry.findtext(f"{namespace}{name}") or "")
