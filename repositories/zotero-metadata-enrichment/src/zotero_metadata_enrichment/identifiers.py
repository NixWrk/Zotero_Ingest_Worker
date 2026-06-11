from __future__ import annotations

import html
import re


def extract_doi_from_text(text: str) -> str | None:
    match = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", text, re.IGNORECASE)
    if not match:
        return None
    return normalize_doi(match.group(0))


def normalize_doi(value: str) -> str:
    value = html.unescape(str(value or "")).strip()
    value = re.sub(r"^(?:doi:\s*|https?://(?:dx\.)?doi\.org/)", "", value, flags=re.IGNORECASE)
    return value.strip().rstrip(".,;:)]}")


def extract_arxiv_id_from_text(text: str) -> str | None:
    patterns = [
        r"(?i)\b10\.48550/arxiv\.([A-Za-z.-]+/\d{7}|\d{4}\.\d{4,5})(v\d+)?\b",
        r"(?i)\barxiv\s*:\s*([A-Za-z.-]+/\d{7}|\d{4}\.\d{4,5})(v\d+)?\b",
        r"(?i)\barxiv\.org/(?:abs|pdf|html)/([A-Za-z.-]+/\d{7}|\d{4}\.\d{4,5})(v\d+)?(?:\.pdf)?",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return normalize_arxiv_id("".join(part or "" for part in match.groups()))
    return None


def normalize_arxiv_id(value: str) -> str:
    value = html.unescape(str(value or "")).strip()
    value = re.sub(r"(?i)^arxiv:\s*", "", value)
    value = re.sub(r"(?i)^https?://arxiv\.org/(?:abs|pdf|html)/", "", value)
    value = re.sub(r"(?i)^10\.48550/arxiv\.", "", value)
    value = value.strip().rstrip(".,;:)]}")
    value = re.sub(r"(?i)\.pdf$", "", value)
    return re.sub(r"(?i)v\d+$", "", value)


def extract_pmid_from_text(text: str) -> str | None:
    patterns = [
        r"(?i)\bPMID\s*[:#]?\s*(\d{5,10})\b",
        r"(?i)\bpubmed(?:\s+id)?\s*[:#]?\s*(\d{5,10})\b",
        r"(?i)\bpubmed\.ncbi\.nlm\.nih\.gov/(\d{5,10})\b",
        r"(?i)\bncbi\.nlm\.nih\.gov/pubmed/(\d{5,10})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return normalize_pmid(match.group(1))
    return None


def normalize_pmid(value: str) -> str:
    value = html.unescape(str(value or "")).strip()
    value = re.sub(r"(?i)^PMID\s*[:#]?\s*", "", value)
    value = re.sub(r"(?i)^pubmed(?:\s+id)?\s*[:#]?\s*", "", value)
    value = value.strip().rstrip(".,;:)]}")
    match = re.search(r"\d{1,10}", value)
    if not match:
        return ""
    pmid = match.group(0).lstrip("0")
    return pmid if pmid and pmid != "0" else ""


def extract_pmcid_from_text(text: str) -> str | None:
    patterns = [
        r"(?i)\b(PMC\d{5,10})\b",
        r"(?i)\bpmc\.ncbi\.nlm\.nih\.gov/articles/(PMC\d{5,10})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return normalize_pmcid(match.group(1))
    return None


def normalize_pmcid(value: str) -> str:
    value = html.unescape(str(value or "")).strip()
    match = re.search(r"(?i)PMC\s*(\d{5,10})", value)
    return f"PMC{match.group(1)}" if match else ""


def extract_isbn_from_text(text: str) -> str | None:
    patterns = [
        r"(?i)\bISBN(?:-1[03])?\s*[:#]?\s*((?:97[89][-\s]?)?[0-9][0-9Xx\-\s]{8,20}[0-9Xx])\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            isbn = normalize_isbn(match.group(1))
            if isbn:
                return isbn
    return None


def normalize_isbn(value: str) -> str:
    value = html.unescape(str(value or "")).strip()
    value = re.sub(r"(?i)^ISBN(?:-1[03])?\s*[:#]?\s*", "", value)
    value = re.sub(r"[^0-9Xx]", "", value)
    if len(value) not in {10, 13}:
        return ""
    return value[:-1] + value[-1].upper()
