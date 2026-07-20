from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, cast

from .package_paths import ensure_local_package_paths

ensure_local_package_paths()

from zotero_metadata_enrichment import (
    MetadataCandidate,
    build_metadata_diff as package_build_metadata_diff,
    build_metadata_patch as package_build_metadata_patch,
    extract_arxiv_id_from_text as package_extract_arxiv_id_from_text,
    extract_doi_from_text as package_extract_doi_from_text,
    normalize_arxiv_id as package_normalize_arxiv_id,
    normalize_doi as package_normalize_doi,
)
from zotero_metadata_enrichment.providers.crossref import (
    crossref_work_to_candidate as package_crossref_work_to_candidate,
)
from zotero_metadata_enrichment.providers.zotero_translation_server import (
    zotero_translator_item_to_candidate as package_zotero_translator_item_to_candidate,
)
from zotero_metadata_enrichment.safe_http import (
    UnsafeUrlError,
)
from zotero_metadata_enrichment.text import (
    normalize_space as package_normalize_space,
    title_match_score as package_title_match_score,
)

from . import full_text_discovery
from .filename_safety import safe_filename_component
from .local_zotero import LocalAttachment, LocalItemMetadata


MAX_HTTP_ERROR_BODY_BYTES = 2_000
MAX_HTTP_ERROR_BODY_CHARS = 500
MAX_RESEARCHGATE_URL_CHARS = 4096
MAX_RESEARCHGATE_ENCODED_URL_BYTES = MAX_RESEARCHGATE_URL_CHARS * 12
MAX_SCIHUB_QUERY_CANDIDATES = 16
MAX_SCIHUB_QUERY_CHARS = 512
MAX_SCIHUB_QUERY_TYPE_CHARS = 32
MAX_SCIHUB_QUERY_LIST_BYTES = 24_576


def metadata_job_owner() -> str:
    return f"zotero-worker-metadata:{socket.gethostname()}:{uuid.uuid4().hex}:{os.getpid()}"


def build_metadata_patch(
    candidate: MetadataCandidate,
    *,
    current_fields: dict[str, str],
    policy: str,
) -> dict[str, str]:
    return package_build_metadata_patch(
        candidate, current_fields=current_fields, policy=policy
    )


def build_metadata_diff(
    candidate: MetadataCandidate,
    *,
    current_fields: dict[str, str],
    policy: str,
) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        package_build_metadata_diff(
            candidate, current_fields=current_fields, policy=policy
        ),
    )


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
    "document": frozenset(
        {
            "pages",
        }
    ),
    "patent": frozenset(
        {
            "libraryCatalog",
        }
    ),
}


def filter_metadata_diff_for_item_type(
    diff: dict[str, Any], *, item_type: str | None
) -> dict[str, Any]:
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


def crossref_work_to_candidate(
    work: dict[str, Any], *, score: float
) -> MetadataCandidate | None:
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
        parts.extend(
            [
                attachment.filename,
                str(attachment.zotero_path or ""),
                str(attachment.file_path),
            ]
        )
    return "\n".join(parts)


def _is_nonretryable_worker_error(exc: Exception) -> bool:
    if isinstance(exc, UnsafeUrlError):
        return True
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


def _first_researchgate_browser_fallback(
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    fallbacks = payload.get("browser_fallbacks")
    if not isinstance(fallbacks, list):
        return None
    for item in fallbacks:
        if not isinstance(item, dict):
            continue
        url_value = item.get("url")
        url = url_value.strip() if isinstance(url_value, str) else ""
        if url and _is_researchgate_url(url):
            return item
    return None


def _is_researchgate_url(url: str) -> bool:
    if (
        len(url) > MAX_RESEARCHGATE_URL_CHARS
        or not url
        or url != url.strip()
        or "\\" in url
        or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in url)
    ):
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme.casefold() != "https":
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if port not in {None, 443}:
        return False
    host = (parsed.hostname or "").strip(".").casefold()
    return host == "researchgate.net" or host.endswith(".researchgate.net")


def _job_queue_key(job: dict[str, Any]) -> str:
    queue_key = job.get("queue_key")
    return queue_key if isinstance(queue_key, str) else ""


def _researchgate_url_from_job(job: dict[str, Any]) -> str:
    queue_key = _job_queue_key(job)
    marker = "|url="
    if marker not in queue_key:
        return ""
    encoded = queue_key.rsplit(marker, 1)[-1].split("|", 1)[0]
    if (
        len(encoded) > MAX_RESEARCHGATE_ENCODED_URL_BYTES
        or len(encoded.encode("utf-8")) > MAX_RESEARCHGATE_ENCODED_URL_BYTES
    ):
        return ""
    url = urllib.parse.unquote(encoded)
    return url if _is_researchgate_url(url) else ""


def _researchgate_result_retryable(result: dict[str, Any]) -> bool:
    status = str(result.get("status") or "").strip()
    terminal_statuses = {
        "attach_invalid_result",
        "download_invalid_result",
        "download_not_pdf",
        "item_key_required_for_attach",
        "item_not_found",
        "network_policy_blocked",
        "parent_already_has_pdf",
        "playwright_missing",
        "preflight_invalid_result",
        "unsafe_browser_url",
    }
    return status not in terminal_statuses


def _first_successful_pdf_download(value: object) -> dict[str, Any] | None:
    if not isinstance(value, list):
        return None
    for item in value:
        if not isinstance(item, dict) or item.get("ok") is not True:
            continue
        output_path = item.get("output_path")
        if isinstance(output_path, str) and output_path.strip():
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
        if len(candidates) >= MAX_SCIHUB_QUERY_CANDIDATES:
            return
        text = _normalize_identifier(str(value or ""))
        if not text or len(text) > MAX_SCIHUB_QUERY_CHARS:
            return
        key = (query_type, text.casefold())
        if key in seen:
            return
        seen.add(key)
        candidate = {"type": query_type, "query": text}
        bounded = _bounded_scihub_query_candidates([*candidates, candidate])
        if len(bounded) > len(candidates):
            candidates.append(candidate)

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
    for match in re.findall(
        r"\bPMID\s*[:#]?\s*(\d{5,12})\b", haystack, flags=re.IGNORECASE
    ):
        add("pmid", match)
    for match in re.findall(
        r"\bpubmed\.ncbi\.nlm\.nih\.gov/(\d{5,12})\b", haystack, flags=re.IGNORECASE
    ):
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


def _bounded_scihub_query_candidates(
    candidates: list[dict[str, str]],
) -> list[dict[str, str]]:
    bounded: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    encoded_bytes = 0
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        raw_query_type = candidate.get("type")
        raw_query = candidate.get("query")
        if not isinstance(raw_query_type, str) or not isinstance(raw_query, str):
            continue
        query_type = _normalize_identifier(raw_query_type)
        query = _normalize_identifier(raw_query)
        if (
            not query_type
            or not query
            or len(query_type) > MAX_SCIHUB_QUERY_TYPE_CHARS
            or len(query) > MAX_SCIHUB_QUERY_CHARS
        ):
            continue
        key = (query_type.casefold(), query.casefold())
        if key in seen:
            continue
        encoded = (
            f"{urllib.parse.quote(query_type, safe='')}"
            f":{urllib.parse.quote(query, safe='')}"
        )
        additional_bytes = len(encoded.encode("utf-8")) + (1 if bounded else 0)
        if encoded_bytes + additional_bytes > MAX_SCIHUB_QUERY_LIST_BYTES:
            continue
        seen.add(key)
        bounded.append({"type": query_type, "query": query})
        encoded_bytes += additional_bytes
        if len(bounded) >= MAX_SCIHUB_QUERY_CANDIDATES:
            break
    return bounded


def _encode_scihub_query_candidates(candidates: list[dict[str, str]]) -> str:
    parts: list[str] = []
    for candidate in _bounded_scihub_query_candidates(candidates):
        query_type = candidate["type"]
        query = candidate["query"]
        parts.append(
            f"{urllib.parse.quote(query_type, safe='')}"
            f":{urllib.parse.quote(query, safe='')}"
        )
    return ",".join(parts)


def _decode_bounded_scihub_component(encoded: str, *, max_chars: int) -> str:
    if (
        len(encoded) > MAX_SCIHUB_QUERY_LIST_BYTES
        or len(encoded.encode("utf-8")) > MAX_SCIHUB_QUERY_LIST_BYTES
    ):
        return ""
    decoded = _normalize_identifier(urllib.parse.unquote(encoded))
    return decoded if len(decoded) <= max_chars else ""


def _scihub_queries_from_job(job: dict[str, Any]) -> list[dict[str, str]]:
    queue_key = _job_queue_key(job)
    marker = "|query_list="
    if marker in queue_key:
        encoded = queue_key.rsplit(marker, 1)[-1].split("|", 1)[0]
        if (
            len(encoded) > MAX_SCIHUB_QUERY_LIST_BYTES
            or len(encoded.encode("utf-8")) > MAX_SCIHUB_QUERY_LIST_BYTES
        ):
            return []
        queries: list[dict[str, str]] = []
        for part in encoded.split(","):
            if not part or ":" not in part:
                continue
            raw_type, raw_query = part.split(":", 1)
            if raw_type:
                query_type = _decode_bounded_scihub_component(
                    raw_type,
                    max_chars=MAX_SCIHUB_QUERY_TYPE_CHARS,
                )
                if not query_type:
                    continue
            else:
                query_type = "doi"
            query = _decode_bounded_scihub_component(
                raw_query,
                max_chars=MAX_SCIHUB_QUERY_CHARS,
            )
            if not query:
                continue
            bounded = _bounded_scihub_query_candidates(
                [*queries, {"type": query_type, "query": query}]
            )
            if len(bounded) > len(queries):
                queries = bounded
            if len(queries) >= MAX_SCIHUB_QUERY_CANDIDATES:
                break
        if queries:
            return queries

    query = _scihub_query_from_job(job)
    if not query:
        return []
    return _bounded_scihub_query_candidates(
        [{"type": _scihub_query_type_from_job(job), "query": query}]
    )


def _scihub_doi_from_job(job: dict[str, Any]) -> str:
    return _scihub_query_from_job(job)


def _scihub_query_from_job(job: dict[str, Any]) -> str:
    queue_key = _job_queue_key(job)
    marker = "|query="
    if marker in queue_key:
        encoded = queue_key.rsplit(marker, 1)[-1].split("|", 1)[0]
    else:
        marker = "|doi="
        if marker not in queue_key:
            return ""
        encoded = queue_key.rsplit(marker, 1)[-1].split("|", 1)[0]
    return _decode_bounded_scihub_component(
        encoded,
        max_chars=MAX_SCIHUB_QUERY_CHARS,
    )


def _scihub_query_type_from_job(job: dict[str, Any]) -> str:
    queue_key = _job_queue_key(job)
    marker = "|query_type="
    if marker not in queue_key:
        return "doi"
    encoded = queue_key.split(marker, 1)[-1].split("|", 1)[0]
    query_type = _decode_bounded_scihub_component(
        encoded,
        max_chars=MAX_SCIHUB_QUERY_TYPE_CHARS,
    )
    if not query_type:
        return "doi"
    return query_type


def _normalize_identifier(value: str) -> str:
    return _normalize_space(str(value or "").strip())


def _scihub_result_retryable(result: dict[str, Any]) -> bool:
    status = str(result.get("status") or "").strip()
    terminal_statuses = {
        "attach_invalid_result",
        "download_artifact_changed",
        "download_invalid_result",
        "identity_invalid_result",
        "identity_mismatch",
        "item_not_found",
        "missing_doi",
        "missing_query",
        "non_pdf",
        "parent_already_has_pdf",
        "too_large",
        "unresolved",
        "unsafe_url",
    }
    if status in terminal_statuses:
        return False
    if status == "http_error":
        http_status = result.get("http_status")
        if type(http_status) is not int:
            return True
        return http_status in {408, 409, 425, 429, 500, 502, 503, 504}
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
    current_lines = {
        line.strip().casefold() for line in current.splitlines() if line.strip()
    }
    new_lines = [line.strip() for line in new_value.splitlines() if line.strip()]
    additions = [line for line in new_lines if line.casefold() not in current_lines]
    if not additions:
        return current
    return current.rstrip() + "\n" + "\n".join(additions)


def _safe_filename(value: str) -> str:
    return safe_filename_component(value, default="document", max_chars=160)


def _patch_digest(fields: dict[str, str]) -> str:
    payload = json.dumps(fields, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def full_text_worker_status(payload: dict[str, Any]) -> str:
    return full_text_discovery.full_text_worker_status(payload)


def first_full_text_output_path(payload: dict[str, Any]) -> str | None:
    return full_text_discovery.first_full_text_output_path(payload)


def _http_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read(MAX_HTTP_ERROR_BODY_BYTES)
    except Exception:
        return str(exc)[:MAX_HTTP_ERROR_BODY_CHARS]
    if not isinstance(raw, (bytes, bytearray)):
        return str(exc)[:MAX_HTTP_ERROR_BODY_CHARS]
    if not raw:
        return str(exc)[:MAX_HTTP_ERROR_BODY_CHARS]
    return bytes(raw).decode("utf-8", errors="replace")[:MAX_HTTP_ERROR_BODY_CHARS]
