from __future__ import annotations

import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

from ..identifiers import normalize_doi, normalize_pmcid, normalize_pmid
from ..models import MetadataCandidate
from ..provider_http import read_json_object, read_text
from ..text import normalize_space, strip_html


class PubMedClient:
    def __init__(
        self,
        *,
        timeout_seconds: int = 60,
        user_agent: str = "zotero-metadata-enrichment/0.1",
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent

    def by_pmid(self, pmid: str) -> MetadataCandidate | None:
        pmid = normalize_pmid(pmid)
        if not pmid:
            return None
        params = {
            "db": "pubmed",
            "id": pmid,
            "retmode": "xml",
        }
        xml_text = self._get_text("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi", params)
        candidates = parse_pubmed_xml(xml_text)
        if not candidates:
            return None
        candidate = candidates[0]
        return MetadataCandidate(
            source="pubmed",
            identifier=pmid,
            score=1.0,
            fields=candidate.fields,
            raw={**candidate.raw, "match": "pmid"},
        )

    def by_pmcid(self, pmcid: str) -> MetadataCandidate | None:
        pmcid = normalize_pmcid(pmcid)
        if not pmcid:
            return None
        params = {
            "ids": pmcid,
            "format": "json",
        }
        payload = self._get_json("https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/", params)
        records = payload.get("records") if isinstance(payload, dict) else None
        record = records[0] if isinstance(records, list) and records else None
        if not isinstance(record, dict):
            return None
        pmid = normalize_pmid(str(record.get("pmid") or ""))
        if pmid:
            candidate = self.by_pmid(pmid)
            if candidate is None:
                return None
            fields = {**candidate.fields}
            fields.setdefault("PMCID", pmcid)
            if record.get("doi"):
                fields.setdefault("DOI", normalize_doi(str(record.get("doi") or "")))
            return MetadataCandidate(
                source="pubmed",
                identifier=pmcid,
                score=1.0,
                fields={key: value for key, value in fields.items() if value},
                raw={**candidate.raw, "match": "pmcid", "idconv": record},
            )
        doi = normalize_doi(str(record.get("doi") or ""))
        fields = {"PMCID": pmcid, "DOI": doi, "url": f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/", "libraryCatalog": "PubMed"}
        return MetadataCandidate(
            source="pubmed",
            identifier=pmcid,
            score=0.9,
            fields={key: value for key, value in fields.items() if value},
            raw={"match": "pmcid", "idconv": record},
        )

    def _get_text(self, url: str, params: dict[str, str]) -> str:
        request = urllib.request.Request(
            f"{url}?{urllib.parse.urlencode(params)}",
            headers={
                "Accept": "application/xml,text/xml;q=0.9,*/*;q=0.8",
                "User-Agent": self.user_agent,
            },
            method="GET",
        )
        return read_text(request, timeout=self.timeout_seconds)

    def _get_json(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{url}?{urllib.parse.urlencode(params)}",
            headers={
                "Accept": "application/json",
                "User-Agent": self.user_agent,
            },
            method="GET",
        )
        return read_json_object(request, timeout=self.timeout_seconds, error_label=url)


def parse_pubmed_xml(xml_text: str) -> list[MetadataCandidate]:
    root = ET.fromstring(xml_text)
    candidates: list[MetadataCandidate] = []
    for article in root.findall(".//PubmedArticle"):
        fields = pubmed_article_fields(article)
        pmid = fields.get("PMID", "")
        if not fields.get("title") and not pmid:
            continue
        raw = {
            "authors": pubmed_authors(article),
            "article_ids": pubmed_article_ids(article),
            "publication_status": text(article.find(".//PublicationStatus")),
        }
        candidates.append(
            MetadataCandidate(
                source="pubmed",
                identifier=pmid,
                score=1.0,
                fields={key: normalize_space(value) for key, value in fields.items() if normalize_space(value)},
                raw=raw,
            )
        )
    return candidates


def pubmed_article_fields(article: ET.Element) -> dict[str, str]:
    article_ids = pubmed_article_ids(article)
    journal = article.find(".//Article/Journal")
    medline = article.find(".//MedlineCitation")
    pmid = text(medline.find("PMID") if medline is not None else None)
    pmcid = article_ids.get("pmc", "")
    if pmcid:
        pmcid = normalize_pmcid(pmcid)
    doi = normalize_doi(article_ids.get("doi", ""))
    publication_title = normalize_space(element_text(journal.find("Title") if journal is not None else None))
    journal_abbrev = normalize_space(element_text(journal.find("ISOAbbreviation") if journal is not None else None))
    issn = text(journal.find("ISSN") if journal is not None else None)
    article_node = article.find(".//Article")
    fields = {
        "title": normalize_space(element_text(article_node.find("ArticleTitle") if article_node is not None else None)),
        "abstractNote": pubmed_abstract(article),
        "date": pubmed_date(article),
        "publicationTitle": publication_title,
        "journalAbbreviation": journal_abbrev,
        "volume": text(article.find(".//JournalIssue/Volume")),
        "issue": text(article.find(".//JournalIssue/Issue")),
        "pages": text(article.find(".//Pagination/MedlinePgn")),
        "ISSN": issn,
        "DOI": doi,
        "PMID": normalize_pmid(pmid),
        "PMCID": pmcid,
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{normalize_pmid(pmid)}/" if normalize_pmid(pmid) else "",
        "libraryCatalog": "PubMed",
    }
    return fields


def pubmed_article_ids(article: ET.Element) -> dict[str, str]:
    result: dict[str, str] = {}
    for node in article.findall(".//PubmedData/ArticleIdList/ArticleId"):
        id_type = str(node.attrib.get("IdType") or "").strip().lower()
        value = text(node)
        if id_type and value:
            result[id_type] = value
    return result


def pubmed_abstract(article: ET.Element) -> str:
    parts: list[str] = []
    for node in article.findall(".//Article/Abstract/AbstractText"):
        value = normalize_space(strip_html(element_text(node)))
        if not value:
            continue
        label = normalize_space(str(node.attrib.get("Label") or ""))
        parts.append(f"{label}: {value}" if label else value)
    return "\n".join(parts)


def pubmed_authors(article: ET.Element) -> list[dict[str, str]]:
    authors: list[dict[str, str]] = []
    for author in article.findall(".//Article/AuthorList/Author"):
        collective = text(author.find("CollectiveName"))
        if collective:
            authors.append({"name": collective})
            continue
        last = text(author.find("LastName"))
        fore = text(author.find("ForeName"))
        initials = text(author.find("Initials"))
        if last or fore or initials:
            authors.append({"firstName": fore, "lastName": last, "initials": initials})
    return authors


def pubmed_date(article: ET.Element) -> str:
    for path in (".//Article/Journal/JournalIssue/PubDate", ".//MedlineCitation/DateCompleted"):
        node = article.find(path)
        if node is None:
            continue
        year = text(node.find("Year"))
        medline_date = text(node.find("MedlineDate"))
        if not year and medline_date:
            match = re.search(r"\d{4}", medline_date)
            year = match.group(0) if match else ""
        if not year:
            continue
        month = month_number(text(node.find("Month")))
        day = text(node.find("Day"))
        result = year
        if month:
            result += f"-{month}"
            if day.isdigit():
                result += f"-{int(day):02d}"
        return result
    return ""


def month_number(value: str) -> str:
    value = normalize_space(value).strip(".")
    if not value:
        return ""
    if value.isdigit():
        number = int(value)
        return f"{number:02d}" if 1 <= number <= 12 else ""
    months = {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "sept": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
    number = months.get(value[:4].casefold()) or months.get(value[:3].casefold())
    return f"{number:02d}" if number else ""


def text(node: ET.Element | None) -> str:
    return normalize_space(element_text(node))


def element_text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return "".join(node.itertext())
