from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any

from .package_paths import ensure_local_package_paths

ensure_local_package_paths()

from zotero_metadata_enrichment import (  # type: ignore[import-not-found]
    MetadataCandidate,
    build_metadata_diff as package_build_metadata_diff,
    build_metadata_patch as package_build_metadata_patch,
    extract_arxiv_id_from_text as package_extract_arxiv_id_from_text,
    extract_doi_from_text as package_extract_doi_from_text,
    normalize_arxiv_id as package_normalize_arxiv_id,
    normalize_doi as package_normalize_doi,
)
from zotero_metadata_enrichment.providers.crossref import (  # type: ignore[import-not-found]
    crossref_work_to_candidate as package_crossref_work_to_candidate,
)
from zotero_metadata_enrichment.providers.zotero_translation_server import (  # type: ignore[import-not-found]
    zotero_translator_item_to_candidate as package_zotero_translator_item_to_candidate,
)
from zotero_metadata_enrichment.text import (  # type: ignore[import-not-found]
    normalize_space as package_normalize_space,
    title_match_score as package_title_match_score,
)

from . import full_text_discovery
from .local_zotero import LocalAttachment, LocalItemMetadata


def metadata_job_owner() -> str:
    return f"zotero-worker-metadata:{socket.gethostname()}:{os.getpid()}"


def build_metadata_patch(
    candidate: MetadataCandidate,
    *,
    current_fields: dict[str, str],
    policy: str,
) -> dict[str, str]:
    return package_build_metadata_patch(candidate, current_fields=current_fields, policy=policy)


def build_metadata_diff(
    candidate: MetadataCandidate,
    *,
    current_fields: dict[str, str],
    policy: str,
) -> dict[str, Any]:
    return package_build_metadata_diff(candidate, current_fields=current_fields, policy=policy)


ITEM_TYPE_UNSUPPORTED_PATCH_FIELDS: dict[str, frozenset[str]] = {
    "preprint": frozenset(
        {
            "ISSN",
            "publicationTitle",
            "journalAbbreviation",
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
            "institution",
            "reportType",
            "reportNumber",
            "conferenceName",
            "proceedingsTitle",
            "websiteTitle",
            "websiteType",
        }
    ),
    "journalArticle": frozenset(
        {
            "ISBN",
            "edition",
            "numPages",
            "numberOfVolumes",
            "bookTitle",
            "institution",
            "reportType",
            "reportNumber",
            "conferenceName",
            "proceedingsTitle",
            "websiteTitle",
            "websiteType",
            "publisher",
            "place",
        }
    ),
    "bookSection": frozenset(
        {
            "ISSN",
            "publicationTitle",
            "journalAbbreviation",
            "volume",
            "issue",
            "numPages",
            "numberOfVolumes",
            "institution",
            "reportType",
            "reportNumber",
            "conferenceName",
            "proceedingsTitle",
            "websiteTitle",
            "websiteType",
        }
    ),
    "conferencePaper": frozenset(
        {
            "ISSN",
            "publicationTitle",
            "journalAbbreviation",
            "volume",
            "issue",
            "ISBN",
            "edition",
            "numPages",
            "numberOfVolumes",
            "bookTitle",
            "institution",
            "reportType",
            "reportNumber",
            "websiteTitle",
            "websiteType",
        }
    ),
    "report": frozenset(
        {
            "ISSN",
            "publicationTitle",
            "journalAbbreviation",
            "volume",
            "issue",
            "ISBN",
            "edition",
            "numPages",
            "numberOfVolumes",
            "bookTitle",
            "conferenceName",
            "proceedingsTitle",
            "publisher",
            "websiteTitle",
            "websiteType",
        }
    ),
    "webpage": frozenset(
        {
            "DOI",
            "ISSN",
            "publicationTitle",
            "journalAbbreviation",
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
            "institution",
            "reportType",
            "reportNumber",
            "conferenceName",
            "proceedingsTitle",
            "libraryCatalog",
        }
    ),
}


def filter_metadata_diff_for_item_type(diff: dict[str, Any], *, item_type: str | None) -> dict[str, Any]:
    unsupported = ITEM_TYPE_UNSUPPORTED_PATCH_FIELDS.get(str(item_type or "").strip())
    if not unsupported:
        return diff

    patch = dict(diff.get("patch") or {})
    skipped_fields = dict(diff.get("skipped_fields") or {})
    removed = sorted(field for field in patch if field in unsupported)
    if not removed:
        return diff

    for field in removed:
        patch.pop(field, None)
        skipped_fields[field] = f"field_not_valid_for_item_type:{item_type}"

    updated = dict(diff)
    updated["patch"] = patch
    updated["skipped_fields"] = skipped_fields
    updated["applied_fields"] = sorted(patch)
    return updated


def extract_doi_from_text(text: str) -> str | None:
    return package_extract_doi_from_text(text)


def normalize_doi(value: str) -> str:
    return package_normalize_doi(value)


def extract_arxiv_id_from_text(text: str) -> str | None:
    return package_extract_arxiv_id_from_text(text)


def normalize_arxiv_id(value: str) -> str:
    return package_normalize_arxiv_id(value)


def crossref_work_to_candidate(work: dict[str, Any], *, score: float) -> MetadataCandidate | None:
    return package_crossref_work_to_candidate(work, score=score)


def zotero_translator_item_to_candidate(
    item: dict[str, Any],
    *,
    source: str,
    identifier: str,
    default_score: float,
    expected_title: str = "",
) -> MetadataCandidate | None:
    return package_zotero_translator_item_to_candidate(
        item,
        source=source,
        identifier=identifier,
        default_score=default_score,
        expected_title=expected_title,
    )


def title_match_score(left: str, right: str) -> float:
    return package_title_match_score(left, right)


def _metadata_haystack(
    metadata: LocalItemMetadata | None,
    attachment: LocalAttachment | None = None,
) -> str:
    parts: list[str] = []
    if metadata is not None:
        parts.extend(str(value) for value in metadata.fields.values() if value)
        parts.extend(str(tag) for tag in metadata.tags)
        for relation in metadata.relations:
            parts.extend(str(value) for value in relation.values() if value)
    if attachment is not None:
        parts.extend([attachment.filename, str(attachment.zotero_path or ""), str(attachment.file_path)])
    return "\n".join(parts)


def _is_nonretryable_worker_error(exc: Exception) -> bool:
    message = str(exc)
    markers = (
        "WEB_API_NOT_CONFIGURED",
        "ZOTERO_API_KEY",
        "ZOTERO_USER_ID",
        "Parent item version is unavailable",
        "ZOTERO_RELAY_URL is required",
    )
    return any(marker in message for marker in markers)


def _title_for_lookup(
    metadata: LocalItemMetadata | None,
    attachment: LocalAttachment,
) -> str:
    if metadata is not None and metadata.title:
        return _normalize_space(metadata.title)
    return _normalize_space(Path(attachment.filename).stem)


def _first_researchgate_browser_fallback(payload: dict[str, Any]) -> dict[str, Any] | None:
    fallbacks = payload.get("browser_fallbacks")
    if not isinstance(fallbacks, list):
        return None
    for item in fallbacks:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if url and _is_researchgate_url(url):
            return item
    return None


def _is_researchgate_url(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except ValueError:
        return False
    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    host = host.split(":", 1)[0]
    return host == "researchgate.net" or host.endswith(".researchgate.net")


def _researchgate_url_from_job(job: dict[str, Any]) -> str:
    queue_key = str(job.get("queue_key") or "")
    marker = "|url="
    if marker not in queue_key:
        return ""
    encoded = queue_key.rsplit(marker, 1)[-1]
    return urllib.parse.unquote(encoded).strip()


def _researchgate_result_retryable(result: dict[str, Any]) -> bool:
    status = str(result.get("status") or "").strip()
    if status in {"playwright_missing", "item_key_required_for_attach", "parent_already_has_pdf"}:
        return False
    return True


def _first_successful_pdf_download(value: object) -> dict[str, Any] | None:
    if not isinstance(value, list):
        return None
    for item in value:
        if isinstance(item, dict) and item.get("ok") and str(item.get("output_path") or "").strip():
            return item
    return None


def _doi_for_scihub(metadata: LocalItemMetadata) -> str:
    for candidate in _scihub_query_candidates(metadata):
        if candidate["type"] == "doi":
            return str(candidate["query"])
    return ""


def _scihub_query_candidates(metadata: LocalItemMetadata) -> list[dict[str, str]]:
    fields = getattr(metadata, "fields", None)
    candidates: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(query_type: str, value: object) -> None:
        text = _normalize_identifier(str(value or ""))
        if not text:
            return
        key = (query_type, text.casefold())
        if key in seen:
            return
        seen.add(key)
        candidates.append({"type": query_type, "query": text})

    if isinstance(fields, dict):
        add("doi", normalize_doi(str(fields.get("DOI") or "")))
        for key in ("PMID", "pmid"):
            add("pmid", fields.get(key))
        for key in ("PMCID", "pmcid"):
            add("pmcid", fields.get(key))
        for key in ("url", "URL"):
            add("url", fields.get(key))
        for key in ("archiveLocation", "archiveID"):
            add("arxiv", fields.get(key))

    haystack = _metadata_haystack(metadata)
    add("doi", normalize_doi(extract_doi_from_text(haystack) or ""))
    for pmcid in re.findall(r"\bPMC\d{4,12}\b", haystack, flags=re.IGNORECASE):
        add("pmcid", pmcid.upper())
    for match in re.findall(r"\bPMID\s*[:#]?\s*(\d{5,12})\b", haystack, flags=re.IGNORECASE):
        add("pmid", match)
    for match in re.findall(r"\bpubmed\.ncbi\.nlm\.nih\.gov/(\d{5,12})\b", haystack, flags=re.IGNORECASE):
        add("pmid", match)
    for match in re.findall(
        r"\barxiv\s*[:/]\s*([a-z.-]+/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?\b",
        haystack,
        flags=re.IGNORECASE,
    ):
        add("arxiv", match)
    for match in re.findall(r"https?://[^\s<>()\"']+", haystack, flags=re.IGNORECASE):
        add("url", match.rstrip(".,;]})"))
    return candidates


def _encode_scihub_query_candidates(candidates: list[dict[str, str]]) -> str:
    parts: list[str] = []
    for candidate in candidates:
        query_type = _normalize_identifier(str(candidate.get("type") or ""))
        query = _normalize_identifier(str(candidate.get("query") or ""))
        if not query_type or not query:
            continue
        parts.append(
            f"{urllib.parse.quote(query_type, safe='')}"
            f":{urllib.parse.quote(query, safe='')}"
        )
    return ",".join(parts)


def _scihub_queries_from_job(job: dict[str, Any]) -> list[dict[str, str]]:
    queue_key = str(job.get("queue_key") or "")
    marker = "|query_list="
    if marker in queue_key:
        encoded = queue_key.rsplit(marker, 1)[-1]
        queries: list[dict[str, str]] = []
        for part in encoded.split(","):
            if not part or ":" not in part:
                continue
            raw_type, raw_query = part.split(":", 1)
            query_type = urllib.parse.unquote(raw_type).strip() or "doi"
            query = urllib.parse.unquote(raw_query).strip()
            if query:
                queries.append({"type": query_type, "query": query})
        if queries:
            return queries

    query = _scihub_query_from_job(job)
    if not query:
        return []
    return [{"type": _scihub_query_type_from_job(job), "query": query}]


def _scihub_doi_from_job(job: dict[str, Any]) -> str:
    return _scihub_query_from_job(job)


def _scihub_query_from_job(job: dict[str, Any]) -> str:
    queue_key = str(job.get("queue_key") or "")
    marker = "|query="
    if marker in queue_key:
        encoded = queue_key.rsplit(marker, 1)[-1]
        return urllib.parse.unquote(encoded).strip()
    marker = "|doi="
    if marker not in queue_key:
        return ""
    encoded = queue_key.rsplit(marker, 1)[-1]
    return urllib.parse.unquote(encoded).strip()


def _scihub_query_type_from_job(job: dict[str, Any]) -> str:
    queue_key = str(job.get("queue_key") or "")
    marker = "|query_type="
    if marker not in queue_key:
        return "doi"
    encoded = queue_key.split(marker, 1)[-1].split("|", 1)[0]
    return urllib.parse.unquote(encoded).strip() or "doi"


def _normalize_identifier(value: str) -> str:
    return _normalize_space(str(value or "").strip())


def _scihub_result_retryable(result: dict[str, Any]) -> bool:
    status = str(result.get("status") or "").strip()
    if status in {
        "parent_already_has_pdf",
        "item_not_found",
        "missing_doi",
        "unresolved",
        "non_pdf",
        "identity_mismatch",
        "unsafe_url",
    }:
        return False
    return True


def _enqueue_result(
    attachment: LocalAttachment,
    classification: str,
    *,
    message: str = "",
    job: dict[str, Any] | None = None,
    parent_metadata: LocalItemMetadata | None = None,
    arxiv_id: str | None = None,
) -> dict[str, Any]:
    return {
        "library_id": attachment.library_id,
        "attachment_key": attachment.key,
        "filename": attachment.filename,
        "file_path": str(attachment.file_path),
        "classification": classification,
        "message": message,
        "job": job,
        "parent_metadata": parent_metadata.to_dict() if parent_metadata else None,
        "arxiv_id": arxiv_id,
    }


def _enqueue_item_result(
    metadata: LocalItemMetadata,
    classification: str,
    *,
    message: str = "",
    job: dict[str, Any] | None = None,
    inventory: dict[str, object] | None = None,
) -> dict[str, Any]:
    return {
        "library_id": metadata.library_id,
        "parent_item_key": metadata.key,
        "item_type": metadata.item_type,
        "title": metadata.title,
        "classification": classification,
        "message": message,
        "job": job,
        "inventory": inventory or {},
    }


def _full_text_ocr_candidates(payload: dict[str, Any]) -> list[str]:
    return full_text_discovery.full_text_ocr_candidates(payload)


def _normalize_space(value: str) -> str:
    return package_normalize_space(value)


def _merge_extra(current: str, new_value: str) -> str:
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


def _safe_filename(value: str) -> str:
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", str(value or "document"))
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:160] or "document"


def _patch_digest(fields: dict[str, str]) -> str:
    payload = json.dumps(fields, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def full_text_worker_status(payload: dict[str, Any]) -> str:
    return full_text_discovery.full_text_worker_status(payload)


def first_full_text_output_path(payload: dict[str, Any]) -> str | None:
    return full_text_discovery.first_full_text_output_path(payload)


def _http_error_body(exc: urllib.error.HTTPError) -> str:
    raw = exc.read()
    return raw.decode("utf-8", errors="replace")[:500] if raw else str(exc)
