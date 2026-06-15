from __future__ import annotations

from typing import Any

from .metadata_jobs import (
    METADATA_JOB_ARXIV_HTML,
    METADATA_JOB_ENRICH,
    METADATA_JOB_FULL_TEXT,
    METADATA_JOB_RESEARCHGATE_PDF,
    METADATA_JOB_SCIHUB_PDF,
)


ROLE_ALL = "all"
ROLE_METADATA = "metadata"
ROLE_FULLTEXT = "fulltext"
WORKER_ROLES = frozenset({ROLE_ALL, ROLE_METADATA, ROLE_FULLTEXT})

METADATA_JOB_TYPES = frozenset({METADATA_JOB_ENRICH})
FULLTEXT_JOB_TYPES = frozenset(
    {
        METADATA_JOB_ARXIV_HTML,
        METADATA_JOB_FULL_TEXT,
        METADATA_JOB_RESEARCHGATE_PDF,
        METADATA_JOB_SCIHUB_PDF,
    }
)

METADATA_POST_ACTION_PATHS = frozenset(
    {
        "/api/zotero/metadata/queue/summary",
        "/api/zotero/metadata/enrich/backlog-scan",
        "/api/zotero/metadata/enrich/queue/drain",
        "/api/zotero/metadata/queue/retry",
        "/api/zotero/metadata/queue/cancel",
    }
)

FULLTEXT_POST_ACTION_PATHS = frozenset(
    {
        "/api/zotero/metadata/queue/summary",
        "/api/zotero/arxiv-html/backlog-scan",
        "/api/zotero/arxiv-html/queue/drain",
        "/api/zotero/full-text/backlog-scan",
        "/api/zotero/full-text/queue/drain",
        "/api/zotero/scihub-pdf/backlog-scan",
        "/api/zotero/researchgate-pdf/queue/drain",
        "/api/zotero/scihub-pdf/queue/drain",
        "/api/zotero/metadata/queue/retry",
        "/api/zotero/metadata/queue/cancel",
    }
)

LEGACY_CONTROLLER_POST_ACTION_PATHS = frozenset(
    {
        "/api/zotero/pipeline/full-run/start",
        "/api/zotero/pipeline/full-run/status",
        "/api/zotero/pipeline/full-run/stop",
    }
)

POST_ACTION_PATHS = (
    METADATA_POST_ACTION_PATHS
    | FULLTEXT_POST_ACTION_PATHS
    | LEGACY_CONTROLLER_POST_ACTION_PATHS
)


def normalize_worker_role(value: object, *, default: str = ROLE_ALL) -> str:
    role = str(value or default).strip().casefold()
    if role in {"files", "file", "native-fulltext", "native_fulltext"}:
        role = ROLE_FULLTEXT
    if role not in WORKER_ROLES:
        raise ValueError(
            f"Unsupported worker role {role!r}. Expected one of: {', '.join(sorted(WORKER_ROLES))}."
        )
    return role


def post_action_paths_for_role(role: str) -> frozenset[str]:
    role = normalize_worker_role(role)
    if role == ROLE_METADATA:
        return METADATA_POST_ACTION_PATHS
    if role == ROLE_FULLTEXT:
        return FULLTEXT_POST_ACTION_PATHS
    return POST_ACTION_PATHS


def role_mode_label(role: str) -> str:
    role = normalize_worker_role(role)
    if role == ROLE_METADATA:
        return "metadata-only"
    if role == ROLE_FULLTEXT:
        return "native-fulltext"
    return "metadata-and-files"


def ensure_role_allows_action(role: str, path: str, payload: dict[str, Any]) -> None:
    role = normalize_worker_role(role)
    if path not in post_action_paths_for_role(role):
        raise PermissionError(f"{role_mode_label(role)} worker does not expose {path}.")
    if path != "/api/zotero/metadata/queue/summary":
        return
    job_type = _optional_job_type(payload)
    if job_type is None:
        return
    if role == ROLE_METADATA and job_type not in METADATA_JOB_TYPES:
        raise PermissionError(f"metadata-only worker cannot serve {job_type!r} queue jobs.")
    if role == ROLE_FULLTEXT and job_type not in FULLTEXT_JOB_TYPES:
        raise PermissionError(f"native-fulltext worker cannot serve {job_type!r} queue jobs.")


def cli_command_role(command: str | None) -> str:
    if command in {
        "arxiv-html-backlog-scan",
        "arxiv-html-drain-queue",
        "full-text-backlog-scan",
        "full-text-drain-queue",
        "researchgate-pdf-drain-queue",
        "scihub-pdf-backlog-scan",
        "scihub-pdf-drain-queue",
    }:
        return ROLE_FULLTEXT
    if command in {
        "metadata-queue",
        "metadata-backlog-scan",
        "metadata-drain-queue",
    }:
        return ROLE_METADATA
    return ROLE_ALL


def _optional_job_type(payload: dict[str, Any]) -> str | None:
    value = payload.get("job_type") or payload.get("type")
    if value is None:
        return None
    text = str(value).strip()
    return text or None
