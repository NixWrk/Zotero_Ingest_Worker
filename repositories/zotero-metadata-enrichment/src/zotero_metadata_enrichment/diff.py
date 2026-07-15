from __future__ import annotations

import re
from .models import MetadataCandidate, MetadataDiff
from .text import normalize_space


PATCH_ALLOWED_FIELDS = frozenset(
    {
        "title",
        "abstractNote",
        "date",
        "language",
        "shortTitle",
        "archive",
        "archiveLocation",
        "libraryCatalog",
        "rights",
        "extra",
        "publicationTitle",
        "journalAbbreviation",
        "DOI",
        "ISSN",
        "PMID",
        "PMCID",
        "volume",
        "issue",
        "pages",
        "series",
        "seriesTitle",
        "publisher",
        "place",
        "ISBN",
        "edition",
        "numPages",
        "numberOfVolumes",
        "bookTitle",
        "url",
        "accessDate",
        "institution",
        "reportType",
        "reportNumber",
        "conferenceName",
        "proceedingsTitle",
        "websiteTitle",
        "websiteType",
        "genre",
    }
)


def build_metadata_patch(
    candidate: MetadataCandidate,
    *,
    current_fields: dict[str, str],
    policy: str,
) -> dict[str, str]:
    return build_metadata_diff(candidate, current_fields=current_fields, policy=policy)["patch"]


def build_metadata_diff(
    candidate: MetadataCandidate,
    *,
    current_fields: dict[str, str],
    policy: str,
) -> MetadataDiff:
    patch: dict[str, str] = {}
    current: dict[str, str] = {}
    candidate_fields: dict[str, str] = {}
    skipped: dict[str, str] = {}
    for field, value in candidate.fields.items():
        if field not in PATCH_ALLOWED_FIELDS:
            skipped[field] = "field_not_allowed_by_relay"
            continue
        text = normalize_space(str(value or ""))
        if not text:
            skipped[field] = "candidate_empty"
            continue
        if field == "extra":
            text = filter_redundant_extra_lines(current_fields, text)
            if not text:
                skipped[field] = "candidate_empty"
                continue
            text = merge_extra(current_fields.get("extra", ""), text)
        current_text = normalize_space(str(current_fields.get(field) or ""))
        current[field] = current_text
        candidate_fields[field] = text
        if policy == "emptyFieldsOnly" and current_fields.get(field) not in (None, ""):
            skipped[field] = "current_field_not_empty"
            continue
        if current_text == text:
            skipped[field] = "unchanged"
            continue
        patch[field] = text
    return {
        "policy": policy,
        "current": current,
        "candidate": candidate_fields,
        "patch": patch,
        "skipped_fields": skipped,
        "applied_fields": sorted(patch),
    }


def merge_extra(current: str, new_value: str) -> str:
    current = str(current or "").strip()
    new_value = str(new_value or "").strip()
    if not current:
        return new_value
    current_lines = {line.strip().casefold() for line in current.splitlines() if line.strip()}
    new_lines = [line.strip() for line in new_value.splitlines() if line.strip()]
    additions = [line for line in new_lines if line.casefold() not in current_lines]
    if not additions:
        return current
    return current.rstrip() + "\n" + "\n".join(additions)


def filter_redundant_extra_lines(current_fields: dict[str, str], value: str) -> str:
    current_pmid = normalize_space(current_fields.get("PMID", ""))
    current_pmcid = normalize_space(current_fields.get("PMCID", "")).casefold()
    current_doi = normalize_space(current_fields.get("DOI", "")).casefold()
    current_isbn = normalize_space(current_fields.get("ISBN", "")).casefold()
    current_arxiv = normalize_space(current_fields.get("archiveLocation", "")).casefold()
    lines: list[str] = []
    for line in str(value or "").splitlines():
        text = line.strip()
        if not text:
            continue
        pmid = re.search(r"(?i)^PMID\s*:\s*(\d{1,10})\s*$", text)
        if pmid and current_pmid and pmid.group(1).lstrip("0") == current_pmid.lstrip("0"):
            continue
        pmcid = re.search(r"(?i)^PMCID\s*:\s*(PMC\d{5,10})\s*$", text)
        if pmcid and current_pmcid and pmcid.group(1).casefold() == current_pmcid:
            continue
        doi = re.search(r"(?i)^DOI\s*:\s*(.+?)\s*$", text)
        if doi and current_doi and doi.group(1).strip().casefold() == current_doi:
            continue
        isbn = re.search(r"(?i)^ISBN(?:-1[03])?\s*:\s*(.+?)\s*$", text)
        if isbn and current_isbn and isbn.group(1).strip().casefold() == current_isbn:
            continue
        arxiv = re.search(r"(?i)^arXiv\s*:\s*([A-Za-z.-]+/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?(?:\s+\[[^\]]+\])?\s*$", text)
        if arxiv and current_arxiv and arxiv.group(1).casefold() == current_arxiv:
            continue
        lines.append(text)
    return "\n".join(lines)
