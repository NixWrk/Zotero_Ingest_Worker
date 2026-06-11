from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import socket
import urllib.error
import urllib.parse
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import Any

from .package_paths import ensure_local_package_paths

ensure_local_package_paths()

from zotero_metadata_enrichment import (  # type: ignore[import-not-found]
    EnricherConfig,
    MetadataCandidate,
    MetadataEnricher,
    discover_and_download_full_text,
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


def metadata_job_owner() -> str:
    return f"zotero-worker-metadata:{socket.gethostname()}:{os.getpid()}"

from .arxiv_html import (
    ArxivHtmlJobService,
    arxiv_html_filename,
    parse_arxiv_atom,
    validate_arxiv_html,
)
from .config import WorkerConfig
from .full_text_attachment import (
    FullTextAttachmentService,
    _best_successful_html_download,
    _html_attachment_source_with_embedded_assets,
    local_attachment_from_relay,
    write_parent_attachment_local_copy,
)
from . import full_text_discovery
from .full_text_inventory import (
    inventory_fingerprint,
    should_skip_full_text_scan,
)
from .local_zotero import LocalAttachment, LocalItemMetadata, LocalZoteroStore
from .local_attachment_sync import sync_parent_metadata_local
from .metadata_jobs import (
    METADATA_JOB_ARXIV_HTML,
    METADATA_JOB_ENRICH,
    METADATA_JOB_FULL_TEXT,
    METADATA_JOB_RESEARCHGATE_PDF,
    METADATA_JOB_SCIHUB_PDF,
    metadata_enricher_config_kwargs,
    metadata_queue_key,
)
from .relay_client import ZoteroRelayClient, relay_url_candidates as _relay_url_candidates
from .researchgate_pdf import ResearchGatePdfOptions, download_and_attach_researchgate_pdf
from .scihub_pdf import SciHubPdfOptions, download_and_attach_scihub_pdf
from .state import FileSignature, PipelineStateStore


class ZoteroMetadataProcessor:
    def __init__(self, config: WorkerConfig):
        self.config = config
        self.state = PipelineStateStore(config.state_db_path)
        self._provider_events: list[dict[str, Any]] = []

    def queue(
        self,
        *,
        job_type: str | None = None,
        statuses: set[str] | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "summary": self.state.metadata_queue_summary(job_type=job_type),
            "jobs": self.state.list_metadata_jobs(
                job_type=job_type,
                statuses=statuses,
                limit=limit,
            ),
        }

    def metadata_backlog_scan(
        self,
        *,
        max_items: int | None = None,
        limit: int | None = None,
        force: bool = False,
        library_id: str | None = None,
        data_dir: str | None = None,
        collection: str | None = None,
    ) -> dict[str, Any]:
        return self._backlog_scan(
            job_type=METADATA_JOB_ENRICH,
            max_items=max_items,
            limit=limit,
            force=force,
            library_id=library_id,
            data_dir=data_dir,
            collection=collection,
        )

    def arxiv_html_backlog_scan(
        self,
        *,
        max_items: int | None = None,
        limit: int | None = None,
        force: bool = False,
        library_id: str | None = None,
        data_dir: str | None = None,
        collection: str | None = None,
    ) -> dict[str, Any]:
        return self._backlog_scan(
            job_type=METADATA_JOB_ARXIV_HTML,
            max_items=max_items,
            limit=limit,
            force=force,
            library_id=library_id,
            data_dir=data_dir,
            collection=collection,
        )

    def full_text_backlog_scan(
        self,
        *,
        max_items: int | None = None,
        limit: int | None = None,
        force: bool = False,
        library_id: str | None = None,
        data_dir: str | None = None,
        collection: str | None = None,
    ) -> dict[str, Any]:
        return self._full_text_backlog_scan(
            max_items=max_items,
            limit=limit,
            force=force,
            library_id=library_id,
            data_dir=data_dir,
            collection=collection,
        )

    def scihub_pdf_backlog_scan(
        self,
        *,
        max_items: int | None = None,
        limit: int | None = None,
        force: bool = False,
        library_id: str | None = None,
        data_dir: str | None = None,
        collection: str | None = None,
    ) -> dict[str, Any]:
        return self._scihub_pdf_backlog_scan(
            max_items=max_items,
            limit=limit,
            force=force,
            library_id=library_id,
            data_dir=data_dir,
            collection=collection,
        )

    def _full_text_backlog_scan(
        self,
        *,
        max_items: int | None,
        limit: int | None,
        force: bool,
        library_id: str | None,
        data_dir: str | None,
        collection: str | None,
    ) -> dict[str, Any]:
        self.config.validate_for_scan()
        scanned = 0
        queued = 0
        skipped = 0
        results: list[dict[str, Any]] = []
        effective_limit = limit if limit is not None and limit > 0 else None

        for library_config in self._library_configs(library_id=library_id, data_dir=data_dir):
            zotero = LocalZoteroStore(library_config)
            scan_limit = max_items if max_items is not None else 1_000_000
            for metadata in zotero.iter_regular_items(
                max_items=scan_limit,
                collection=collection,
            ):
                scanned += 1
                inventory = zotero.item_full_text_inventory(metadata)
                if should_skip_full_text_scan(inventory) and not force:
                    result = _enqueue_item_result(
                        metadata,
                        "html_exists",
                        message="Parent item already has source HTML and PDF attachments.",
                        inventory=inventory,
                    )
                else:
                    result = self._enqueue_parent_full_text_item(
                        zotero=zotero,
                        metadata=metadata,
                        inventory=inventory,
                        force=force,
                        reason="full_text_backlog_scan",
                    )
                results.append(result)
                job = result.get("job") or {}
                if job.get("created") and job.get("status") == "queued":
                    queued += 1
                else:
                    skipped += 1
                if effective_limit is not None and queued >= effective_limit:
                    break
            if effective_limit is not None and queued >= effective_limit:
                break

        return {
            "ok": True,
            "mode": "full_text_backlog_scan",
            "job_type": METADATA_JOB_FULL_TEXT,
            "scanned": scanned,
            "queued": queued,
            "skipped": skipped,
            "queue": self.state.metadata_queue_summary(job_type=METADATA_JOB_FULL_TEXT),
            "results": results,
        }

    def _scihub_pdf_backlog_scan(
        self,
        *,
        max_items: int | None,
        limit: int | None,
        force: bool,
        library_id: str | None,
        data_dir: str | None,
        collection: str | None,
    ) -> dict[str, Any]:
        self.config.validate_for_scan()
        scanned = 0
        queued = 0
        skipped = 0
        results: list[dict[str, Any]] = []
        effective_limit = limit if limit is not None and limit > 0 else None

        for library_config in self._library_configs(library_id=library_id, data_dir=data_dir):
            zotero = LocalZoteroStore(library_config)
            scan_limit = max_items if max_items is not None else 1_000_000
            for metadata in zotero.iter_regular_items(
                max_items=scan_limit,
                collection=collection,
            ):
                scanned += 1
                inventory = zotero.item_full_text_inventory(metadata)
                if inventory.get("has_pdf") and not force:
                    result = _enqueue_item_result(
                        metadata,
                        "pdf_exists",
                        message="Parent item already has a PDF attachment.",
                        inventory=inventory,
                    )
                    skipped += 1
                else:
                    result = self._enqueue_scihub_pdf_jobs_for_item(
                        metadata=metadata,
                        inventory=inventory,
                        reason="scihub_pdf_backlog_scan",
                        force=force,
                    )
                    queued += int(result.get("queued") or 0)
                    if not result.get("queued"):
                        skipped += 1
                results.append(result)
                if effective_limit is not None and queued >= effective_limit:
                    break
            if effective_limit is not None and queued >= effective_limit:
                break

        return {
            "ok": True,
            "mode": "scihub_pdf_backlog_scan",
            "job_type": METADATA_JOB_SCIHUB_PDF,
            "scanned": scanned,
            "queued": queued,
            "skipped": skipped,
            "queue": self.state.metadata_queue_summary(job_type=METADATA_JOB_SCIHUB_PDF),
            "results": results,
        }

    def drain_metadata_queue(
        self,
        *,
        limit: int = 1,
        dry_run: bool = False,
        require_relay: bool = True,
        policy: str | None = None,
    ) -> dict[str, Any]:
        return self._drain_queue(
            job_type=METADATA_JOB_ENRICH,
            limit=limit,
            dry_run=dry_run,
            require_relay=require_relay,
            policy=policy,
        )

    def drain_arxiv_html_queue(
        self,
        *,
        limit: int = 1,
        dry_run: bool = False,
        require_relay: bool = True,
    ) -> dict[str, Any]:
        return self._drain_queue(
            job_type=METADATA_JOB_ARXIV_HTML,
            limit=limit,
            dry_run=dry_run,
            require_relay=require_relay,
            policy=None,
        )

    def drain_full_text_queue(
        self,
        *,
        limit: int = 1,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self._drain_queue(
            job_type=METADATA_JOB_FULL_TEXT,
            limit=limit,
            dry_run=dry_run,
            require_relay=False,
            policy=None,
        )

    def drain_researchgate_pdf_queue(
        self,
        *,
        limit: int = 1,
        dry_run: bool = False,
        require_relay: bool = True,
    ) -> dict[str, Any]:
        return self._drain_queue(
            job_type=METADATA_JOB_RESEARCHGATE_PDF,
            limit=limit,
            dry_run=dry_run,
            require_relay=require_relay,
            policy=None,
        )

    def drain_scihub_pdf_queue(
        self,
        *,
        limit: int = 1,
        dry_run: bool = False,
        require_relay: bool = True,
    ) -> dict[str, Any]:
        return self._drain_queue(
            job_type=METADATA_JOB_SCIHUB_PDF,
            limit=limit,
            dry_run=dry_run,
            require_relay=require_relay,
            policy=None,
        )

    def _backlog_scan(
        self,
        *,
        job_type: str,
        max_items: int | None,
        limit: int | None,
        force: bool,
        library_id: str | None,
        data_dir: str | None,
        collection: str | None,
    ) -> dict[str, Any]:
        self.config.validate_for_scan()
        scanned = 0
        queued = 0
        skipped = 0
        results: list[dict[str, Any]] = []
        effective_limit = limit if limit is not None and limit > 0 else None

        for library_config in self._library_configs(library_id=library_id, data_dir=data_dir):
            zotero = LocalZoteroStore(library_config)
            scan_limit = max_items if max_items is not None else max(
                zotero.count_sqlite_pdf_attachments(),
                1_000_000,
            )
            attachments = (
                zotero.iter_collection_pdf_attachments(
                    collection=collection,
                    max_items=scan_limit,
                )
                if collection
                else zotero.iter_pdf_attachments(max_items=scan_limit)
            )
            for attachment in attachments:
                scanned += 1
                result = self._enqueue_attachment(
                    zotero=zotero,
                    attachment=attachment,
                    job_type=job_type,
                    force=force,
                    reason=f"{job_type}_backlog_scan",
                )
                results.append(result)
                job = result.get("job") or {}
                if job.get("created") and job.get("status") == "queued":
                    queued += 1
                else:
                    skipped += 1
                if effective_limit is not None and queued >= effective_limit:
                    break
            if effective_limit is not None and queued >= effective_limit:
                break

        return {
            "ok": True,
            "mode": f"{job_type}_backlog_scan",
            "job_type": job_type,
            "scanned": scanned,
            "queued": queued,
            "skipped": skipped,
            "queue": self.state.metadata_queue_summary(job_type=job_type),
            "results": results,
        }

    def _enqueue_attachment(
        self,
        *,
        zotero: LocalZoteroStore,
        attachment: LocalAttachment,
        job_type: str,
        reason: str,
        force: bool,
    ) -> dict[str, Any]:
        signature = FileSignature.from_path(attachment.file_path)
        parent_metadata = zotero.get_parent_metadata_for_attachment(attachment)
        title = _title_for_lookup(parent_metadata, attachment)
        arxiv_id = extract_arxiv_id_from_text(_metadata_haystack(parent_metadata, attachment))

        if job_type == METADATA_JOB_ARXIV_HTML and not arxiv_id and not title:
            return _enqueue_result(
                attachment,
                "no_metadata_for_arxiv_lookup",
                message="No arXiv identifier or title was available for arXiv lookup.",
                parent_metadata=parent_metadata,
            )

        queue_key = self._queue_key(job_type)
        parent_item_key = parent_metadata.key if parent_metadata else attachment.parent_key
        parent_version = parent_metadata.version if parent_metadata else None
        if job_type == METADATA_JOB_ENRICH and parent_item_key and not force:
            existing = self.state.get_metadata_job_by_parent_scope(
                job_type=METADATA_JOB_ENRICH,
                library_id=attachment.library_id,
                parent_item_key=parent_item_key,
                parent_version=parent_version,
                queue_key=queue_key,
                force=False,
                statuses={"queued", "running", "succeeded", "skipped", "failed_retryable", "failed_final"},
            )
            if existing is not None:
                return _enqueue_result(
                    attachment,
                    "already_known",
                    job={**existing, "created": False},
                    parent_metadata=parent_metadata,
                    arxiv_id=arxiv_id,
                )
        job = self.state.enqueue_metadata_job(
            job_type=job_type,
            library_id=attachment.library_id,
            attachment_key=attachment.key,
            data_dir=attachment.data_dir,
            source_path=attachment.file_path,
            signature=signature,
            status="queued",
            reason=reason,
            force=force,
            parent_item_key=parent_item_key,
            parent_version=parent_version,
            queue_key=queue_key,
        )
        return _enqueue_result(
            attachment,
            "queued" if job.get("created") else "already_known",
            job=job,
            parent_metadata=parent_metadata,
            arxiv_id=arxiv_id,
        )

    def _enqueue_parent_full_text_item(
        self,
        *,
        zotero: LocalZoteroStore,
        metadata: LocalItemMetadata,
        inventory: dict[str, object],
        reason: str,
        force: bool,
    ) -> dict[str, Any]:
        source_path = zotero.config.zotero_sqlite_path
        signature = FileSignature.from_path(source_path)
        queue_key = f"{self._queue_key(METADATA_JOB_FULL_TEXT)}|inventory={inventory_fingerprint(inventory)}"
        if not force:
            existing = self.state.get_metadata_job_by_parent_scope(
                job_type=METADATA_JOB_FULL_TEXT,
                library_id=metadata.library_id,
                parent_item_key=metadata.key,
                parent_version=metadata.version,
                queue_key=queue_key,
                force=False,
                statuses={"queued", "running", "succeeded", "failed_retryable", "failed_final"},
            )
            if existing is not None:
                return _enqueue_item_result(
                    metadata,
                    "already_known",
                    job={**existing, "created": False},
                    inventory=inventory,
                )
        job = self.state.enqueue_metadata_job(
            job_type=METADATA_JOB_FULL_TEXT,
            library_id=metadata.library_id,
            attachment_key=metadata.key,
            data_dir=metadata.data_dir,
            source_path=source_path,
            signature=signature,
            status="queued",
            reason=reason,
            force=force,
            parent_item_key=metadata.key,
            parent_version=metadata.version,
            queue_key=queue_key,
        )
        return _enqueue_item_result(
            metadata,
            "queued" if job.get("created") else "already_known",
            job=job,
            inventory=inventory,
        )

    def _enqueue_researchgate_pdf_fallback(
        self,
        *,
        metadata: LocalItemMetadata,
        payload: dict[str, Any],
        reason: str,
    ) -> dict[str, Any] | None:
        fallback = _first_researchgate_browser_fallback(payload)
        if fallback is None:
            return None
        url = str(fallback.get("url") or "").strip()
        if not url:
            return None
        source_path = Path(metadata.data_dir) / "zotero.sqlite"
        signature = FileSignature.from_path(source_path)
        queue_key = f"{self._queue_key(METADATA_JOB_RESEARCHGATE_PDF)}|url={urllib.parse.quote(url, safe='')}"
        if metadata.version is not None:
            existing = self.state.get_metadata_job_by_parent_scope(
                job_type=METADATA_JOB_RESEARCHGATE_PDF,
                library_id=metadata.library_id,
                parent_item_key=metadata.key,
                parent_version=metadata.version,
                queue_key=queue_key,
                force=False,
                statuses={"queued", "running", "succeeded", "failed_retryable", "failed_final"},
            )
            if existing is not None:
                return {
                    "classification": "already_known",
                    "job": existing,
                    "url": url,
                    "fallback": fallback,
                }
        job = self.state.enqueue_metadata_job(
            job_type=METADATA_JOB_RESEARCHGATE_PDF,
            library_id=metadata.library_id,
            attachment_key=metadata.key,
            data_dir=metadata.data_dir,
            source_path=source_path,
            signature=signature,
            status="queued",
            reason=reason,
            force=force,
            parent_item_key=metadata.key,
            parent_version=metadata.version,
            queue_key=queue_key,
        )
        return {
            "classification": "queued" if job.get("created") else "already_known",
            "job": job,
            "url": url,
            "fallback": fallback,
        }

    def _enqueue_scihub_pdf_fallback(
        self,
        *,
        metadata: LocalItemMetadata,
        payload: dict[str, Any],
        researchgate_enqueue: dict[str, Any] | None,
        reason: str,
    ) -> dict[str, Any] | None:
        if not getattr(self.config, "scihub_enabled", False):
            return None
        # Sci-Hub is only queued immediately when ordinary providers produced
        # no PDF and no browser-only PDF fallback remains. ResearchGate is
        # drained first; a later Sci-Hub backlog pass covers items still without
        # PDF after that browser pass.
        if researchgate_enqueue is not None:
            return None
        inventory = payload.get("existing_full_text_inventory")
        if isinstance(inventory, dict) and inventory.get("has_pdf"):
            return None
        if _first_successful_pdf_download(payload.get("pdf_downloads")) is not None:
            return None
        result = self._enqueue_scihub_pdf_jobs_for_item(
            metadata=metadata,
            inventory=inventory if isinstance(inventory, dict) else {},
            reason=reason,
            force=False,
        )
        if not result.get("queued") and not result.get("jobs"):
            return {"classification": result["classification"], "reason": result.get("message")}
        return result

    def _enqueue_scihub_pdf_jobs_for_item(
        self,
        *,
        metadata: LocalItemMetadata,
        inventory: dict[str, object],
        reason: str,
        force: bool,
    ) -> dict[str, Any]:
        candidates = _scihub_query_candidates(metadata)
        if not candidates:
            return _enqueue_item_result(
                metadata,
                "missing_identifier",
                message="No DOI, PMID, PMCID, arXiv id, or URL is available for Sci-Hub lookup.",
                inventory=inventory,
            )

        jobs: list[dict[str, Any]] = []
        queued = 0
        for candidate in candidates:
            enqueue = self._enqueue_scihub_pdf_query_job(
                metadata=metadata,
                query=str(candidate["query"]),
                query_type=str(candidate["type"]),
                reason=reason,
                force=force,
            )
            if enqueue is None:
                continue
            jobs.append(enqueue)
            if enqueue.get("classification") == "queued":
                queued += 1

        classification = "queued" if queued else "already_known"
        return _enqueue_item_result(
            metadata,
            classification,
            job=jobs[0].get("job") if len(jobs) == 1 else None,
            inventory=inventory,
            message=f"Sci-Hub PDF fallback candidates: {len(candidates)}.",
        ) | {
            "queued": queued,
            "jobs": jobs,
            "scihub_queries": candidates,
        }

    def _enqueue_scihub_pdf_query_job(
        self,
        *,
        metadata: LocalItemMetadata,
        query: str,
        query_type: str,
        reason: str,
        force: bool,
    ) -> dict[str, Any] | None:
        query = _normalize_identifier(query)
        if not query:
            return None

        source_path = Path(metadata.data_dir) / "zotero.sqlite"
        signature = FileSignature.from_path(source_path)
        queue_key = (
            f"{self._queue_key(METADATA_JOB_SCIHUB_PDF)}"
            f"|query_type={urllib.parse.quote(query_type, safe='')}"
            f"|query={urllib.parse.quote(query, safe='')}"
        )
        if metadata.version is not None:
            existing = self.state.get_metadata_job_by_parent_scope(
                job_type=METADATA_JOB_SCIHUB_PDF,
                library_id=metadata.library_id,
                parent_item_key=metadata.key,
                parent_version=metadata.version,
                queue_key=queue_key,
                force=force,
                statuses={"queued", "running", "succeeded", "failed_retryable", "failed_final"},
            )
            if existing is not None:
                return {
                    "classification": "already_known",
                    "job": existing,
                    "query": query,
                    "query_type": query_type,
                }
        job = self.state.enqueue_metadata_job(
            job_type=METADATA_JOB_SCIHUB_PDF,
            library_id=metadata.library_id,
            attachment_key=metadata.key,
            data_dir=metadata.data_dir,
            source_path=source_path,
            signature=signature,
            status="queued",
            reason=reason,
            force=False,
            parent_item_key=metadata.key,
            parent_version=metadata.version,
            queue_key=queue_key,
        )
        return {
            "classification": "queued" if job.get("created") else "already_known",
            "job": job,
            "query": query,
            "query_type": query_type,
        }

    def _drain_queue(
        self,
        *,
        job_type: str,
        limit: int,
        dry_run: bool,
        require_relay: bool,
        policy: str | None,
    ) -> dict[str, Any]:
        if dry_run:
            jobs = self.state.list_metadata_jobs(
                job_type=job_type,
                statuses={"queued"},
                limit=max(limit, 1),
            )
            return {
                "ok": True,
                "dry_run": True,
                "job_type": job_type,
                "would_process": len(jobs),
                "queue": self.state.metadata_queue_summary(job_type=job_type),
                "jobs": jobs,
            }

        if job_type == METADATA_JOB_ENRICH and require_relay and not self.config.zotero_relay_url:
            return {
                "ok": False,
                "error": "ZOTERO_RELAY_URL is required before metadata can be written back.",
                "queue": self.state.metadata_queue_summary(job_type=job_type),
            }

        if job_type == METADATA_JOB_ARXIV_HTML and require_relay and self.config.arxiv_html_attach:
            if not self.config.zotero_relay_url:
                return {
                    "ok": False,
                    "error": "ZOTERO_RELAY_URL is required before arXiv HTML can be attached.",
                    "queue": self.state.metadata_queue_summary(job_type=job_type),
                }

        if job_type == METADATA_JOB_RESEARCHGATE_PDF and require_relay and not self.config.zotero_relay_url:
            return {
                "ok": False,
                "error": "ZOTERO_RELAY_URL is required before ResearchGate PDFs can be attached.",
                "queue": self.state.metadata_queue_summary(job_type=job_type),
            }

        if job_type == METADATA_JOB_SCIHUB_PDF and require_relay and not self.config.zotero_relay_url:
            return {
                "ok": False,
                "error": "ZOTERO_RELAY_URL is required before Sci-Hub PDFs can be attached.",
                "queue": self.state.metadata_queue_summary(job_type=job_type),
            }

        processed = 0
        failed = 0
        results: list[dict[str, Any]] = []
        recovered = self.state.recover_expired_metadata_jobs(job_type=job_type)
        owner = metadata_job_owner()
        lease_seconds = max(int(getattr(self.config, "metadata_job_lease_seconds", 900)), 60)
        effective_limit = limit if limit > 0 else 1_000_000

        while processed < effective_limit:
            job = self.state.lease_next_metadata_job(
                job_type=job_type,
                owner=owner,
                lease_seconds=lease_seconds,
            )
            if job is None:
                break
            if job_type == METADATA_JOB_ENRICH:
                result = self._drain_enrich_job(
                    job,
                    require_relay=require_relay,
                    policy=policy or self.config.metadata_policy,
                )
            elif job_type == METADATA_JOB_ARXIV_HTML:
                result = self._drain_arxiv_html_job(job, require_relay=require_relay)
            elif job_type == METADATA_JOB_FULL_TEXT:
                result = self._drain_full_text_job(job)
            elif job_type == METADATA_JOB_RESEARCHGATE_PDF:
                result = self._drain_researchgate_pdf_job(job)
            elif job_type == METADATA_JOB_SCIHUB_PDF:
                result = self._drain_scihub_pdf_job(job)
            else:
                result = self.state.mark_metadata_job_failed(
                    job_id=str(job["job_id"]),
                    message=f"Unknown metadata job type: {job_type}",
                    retryable=False,
                )
            results.append(result)
            processed += 1
            if result.get("status") in {"failed_retryable", "failed_final"}:
                failed += 1

        return {
            "ok": failed == 0,
            "mode": f"{job_type}_drain_queue",
            "job_type": job_type,
            "processed": processed,
            "failed": failed,
            "recovered": recovered,
            "queue": self.state.metadata_queue_summary(job_type=job_type),
            "results": results,
        }

    def _drain_enrich_job(
        self,
        job: dict[str, Any],
        *,
        require_relay: bool,
        policy: str,
    ) -> dict[str, Any]:
        job_id = str(job["job_id"])
        try:
            attachment = self._attachment_for_job(job)
            zotero = LocalZoteroStore(self._config_for_job(job))
            metadata = zotero.get_parent_metadata_for_attachment(attachment)
            if metadata is None:
                return self.state.mark_metadata_job_skipped(
                    job_id=job_id,
                    message="PDF attachment has no parent item to patch.",
                    result={"reason": "no_parent_item", "attachment": attachment.to_dict()},
                )

            self._provider_events = []
            candidate = self._lookup_metadata_candidate(metadata=metadata, attachment=attachment)
            if candidate is None:
                return self.state.mark_metadata_job_skipped(
                    job_id=job_id,
                    message="No confident metadata candidate was found.",
                    result={
                        "reason": "no_confident_candidate",
                        "metadata": metadata.to_dict(),
                        "provider_events": list(self._provider_events),
                    },
                )

            diff = build_metadata_diff(candidate, current_fields=metadata.fields, policy=policy)
            diff = filter_metadata_diff_for_item_type(diff, item_type=metadata.item_type)
            patch = diff["patch"]
            if not patch:
                return self.state.mark_metadata_job_skipped(
                    job_id=job_id,
                    message="Metadata candidate did not contain patchable fields.",
                    result={
                        "candidate": candidate.to_dict(),
                        "diff": diff,
                        "policy": policy,
                        "provider_events": list(self._provider_events),
                    },
                )
            if metadata.version is None:
                return self.state.mark_metadata_job_failed(
                    job_id=job_id,
                    message="Parent item version is unavailable; cannot safely PATCH Zotero metadata.",
                    retryable=False,
                )

            relay_result: dict[str, Any] | None = None
            if self.config.zotero_relay_url:
                relay_result = self._patch_parent_metadata_via_relay(
                    attachment=attachment,
                    metadata=metadata,
                    fields=patch,
                    policy=policy,
                )
            elif require_relay:
                raise RuntimeError("ZOTERO_RELAY_URL is required before metadata can be written back.")
            local_metadata: dict[str, Any] | None = None
            if relay_result is not None and relay_result.get("ok"):
                try:
                    local_metadata = sync_parent_metadata_local(
                        metadata=metadata,
                        fields=patch,
                        relay_result=relay_result,
                    )
                except Exception as exc:
                    local_metadata = {
                        "ok": False,
                        "error": str(exc),
                        "item_key": metadata.key,
                    }

            result = {
                "attachment_key": attachment.key,
                "parent_item_key": metadata.key,
                "candidate": candidate.to_dict(),
                "current": diff["current"],
                "diff": diff,
                "patch": patch,
                "policy": policy,
                "provider_events": list(self._provider_events),
                "relay": relay_result,
                "local_metadata": local_metadata,
            }
            return self.state.mark_metadata_job_succeeded(
                job_id=job_id,
                message=f"Metadata enrichment completed from {candidate.source}.",
                result=result,
                relay_result=relay_result,
            )
        except urllib.error.HTTPError as exc:
            return self.state.mark_metadata_job_failed(
                job_id=job_id,
                message=f"HTTP {exc.code} while enriching metadata: {_http_error_body(exc)}",
                retryable=exc.code in {408, 409, 425, 429, 500, 502, 503, 504},
            )
        except Exception as exc:
            return self.state.mark_metadata_job_failed(
                job_id=job_id,
                message=str(exc),
                retryable=not _is_nonretryable_worker_error(exc),
            )

    def _drain_full_text_job(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = str(job["job_id"])
        try:
            context = self._full_text_context_for_job(job)
            if context is None:
                return self.state.mark_metadata_job_skipped(
                    job_id=job_id,
                    message="Full-text discovery job has no usable parent item.",
                    result={"reason": "no_parent_item", "job": job},
                )
            attachment, metadata, inventory, source_path, context_kind = context
            output_dir = (
                self._full_text_output_dir_for_item(metadata=metadata, source_path=source_path)
                if context_kind == "parent_item"
                else self._full_text_output_dir(attachment=attachment, source_pdf=source_path)
            )
            arxiv_service = ArxivHtmlJobService(self.config)
            payload = full_text_discovery.FullTextDiscoveryOrchestrator(
                config=self.config,
                metadata_config=self._metadata_enricher().config,
                discover_full_text=discover_and_download_full_text,
                fetch_arxiv_html=arxiv_service.fetch_html,
            ).discover_payload(
                attachment=attachment,
                metadata=metadata,
                inventory=inventory,
                output_dir=output_dir,
                source_context=context_kind,
            )
            attach_result = self._attach_full_text_result(
                attachment=attachment,
                metadata=metadata,
                inventory=inventory,
                payload=payload,
            )
            if attach_result is not None:
                payload["relay_attachment"] = attach_result
            researchgate_enqueue = self._enqueue_researchgate_pdf_fallback(
                metadata=metadata,
                payload=payload,
                reason="full_text_researchgate_browser_fallback",
            )
            if researchgate_enqueue is not None:
                payload["researchgate_pdf_enqueue"] = researchgate_enqueue
            scihub_enqueue = self._enqueue_scihub_pdf_fallback(
                metadata=metadata,
                payload=payload,
                researchgate_enqueue=researchgate_enqueue,
                reason="full_text_scihub_pdf_fallback",
            )
            if scihub_enqueue is not None:
                payload["scihub_pdf_enqueue"] = scihub_enqueue
            return self.state.mark_metadata_job_succeeded(
                job_id=job_id,
                message=f"Full-text discovery finished with status {payload['worker_status']}.",
                result=payload,
                output_path=first_full_text_output_path(payload),
            )
        except urllib.error.HTTPError as exc:
            return self.state.mark_metadata_job_failed(
                job_id=job_id,
                message=f"HTTP {exc.code} while discovering full text: {_http_error_body(exc)}",
                retryable=exc.code in {408, 409, 425, 429, 500, 502, 503, 504},
            )
        except Exception as exc:
            return self.state.mark_metadata_job_failed(
                job_id=job_id,
                message=str(exc),
                retryable=not _is_nonretryable_worker_error(exc),
            )

    def _drain_researchgate_pdf_job(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = str(job["job_id"])
        try:
            url = _researchgate_url_from_job(job)
            if not url:
                return self.state.mark_metadata_job_skipped(
                    job_id=job_id,
                    message="ResearchGate PDF job has no URL.",
                    result={"reason": "missing_researchgate_url", "job": job},
                )
            item_key = str(job.get("parent_item_key") or job.get("attachment_key") or "").strip()
            if not item_key:
                return self.state.mark_metadata_job_skipped(
                    job_id=job_id,
                    message="ResearchGate PDF job has no parent item key.",
                    result={"reason": "missing_parent_item_key", "url": url, "job": job},
                )
            result = asyncio.run(
                download_and_attach_researchgate_pdf(
                    self.config,
                    ResearchGatePdfOptions(
                        url=url,
                        item_key=item_key,
                        data_dir=str(job.get("data_dir") or ""),
                        headless=True,
                        manual_timeout_seconds=0,
                    ),
                )
            )
            if result.get("ok"):
                download = result.get("download")
                output_path = None
                if isinstance(download, dict):
                    output_path = str(download.get("output_path") or "").strip() or None
                return self.state.mark_metadata_job_succeeded(
                    job_id=job_id,
                    message=f"ResearchGate PDF job finished with status {result.get('status')}.",
                    result=result,
                    output_path=output_path,
                    relay_result=result.get("attach") if isinstance(result.get("attach"), dict) else None,
                )
            return self.state.mark_metadata_job_failed(
                job_id=job_id,
                message=str(result.get("error") or result.get("status") or "ResearchGate PDF download failed."),
                retryable=_researchgate_result_retryable(result),
            )
        except Exception as exc:
            return self.state.mark_metadata_job_failed(
                job_id=job_id,
                message=str(exc),
                retryable=not _is_nonretryable_worker_error(exc),
            )

    def _drain_scihub_pdf_job(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = str(job["job_id"])
        try:
            # The query may be a DOI, PMID, PMCID, URL, or other identifier.
            # When it is absent the adapter derives the best DOI from metadata.
            query = _scihub_query_from_job(job)
            query_type = _scihub_query_type_from_job(job)
            item_key = str(job.get("parent_item_key") or job.get("attachment_key") or "").strip()
            if not item_key:
                return self.state.mark_metadata_job_skipped(
                    job_id=job_id,
                    message="Sci-Hub PDF job has no parent item key.",
                    result={
                        "reason": "missing_parent_item_key",
                        "query": query,
                        "query_type": query_type,
                        "job": job,
                    },
                )
            result = download_and_attach_scihub_pdf(
                self.config,
                SciHubPdfOptions(
                    doi=query,
                    item_key=item_key,
                    data_dir=str(job.get("data_dir") or ""),
                    # Download under the shared OCR data root so the file is
                    # visible to zotero-file-relay (see shared_relay_path).
                    output_dir=Path(self.config.ingest_data_root) / "scihub_downloads",
                    mirrors=tuple(self.config.scihub_mirrors),
                    user_agent=self.config.scihub_user_agent,
                    timeout_seconds=self.config.scihub_request_timeout_seconds,
                ),
            )
            result["query"] = query
            result["query_type"] = query_type
            if result.get("ok"):
                download = result.get("download")
                output_path = None
                if isinstance(download, dict):
                    output_path = str(download.get("output_path") or "").strip() or None
                return self.state.mark_metadata_job_succeeded(
                    job_id=job_id,
                    message=f"Sci-Hub PDF job finished with status {result.get('status')}.",
                    result=result,
                    output_path=output_path,
                    relay_result=result.get("attach") if isinstance(result.get("attach"), dict) else None,
                )
            return self.state.mark_metadata_job_failed(
                job_id=job_id,
                message=str(result.get("error") or result.get("status") or "Sci-Hub PDF download failed."),
                retryable=_scihub_result_retryable(result),
            )
        except Exception as exc:
            return self.state.mark_metadata_job_failed(
                job_id=job_id,
                message=str(exc),
                retryable=not _is_nonretryable_worker_error(exc),
            )

    def _drain_arxiv_html_job(self, job: dict[str, Any], *, require_relay: bool) -> dict[str, Any]:
        job_id = str(job["job_id"])
        try:
            attachment = self._attachment_for_job(job)
            zotero = LocalZoteroStore(self._config_for_job(job))
            metadata = zotero.get_parent_metadata_for_attachment(attachment)
            arxiv_service = ArxivHtmlJobService(self.config)
            candidate = arxiv_service.lookup_candidate(metadata=metadata, attachment=attachment)
            if candidate is None:
                return self.state.mark_metadata_job_skipped(
                    job_id=job_id,
                    message="No confident arXiv match was found.",
                    result={
                        "reason": "no_confident_arxiv_match",
                        "metadata": metadata.to_dict() if metadata else None,
                        "attachment": attachment.to_dict(),
                        "provider_events": list(arxiv_service.provider_events),
                    },
                )

            arxiv_id = candidate.identifier
            try:
                html_text = arxiv_service.fetch_html(arxiv_id)
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return self.state.mark_metadata_job_skipped(
                        job_id=job_id,
                        message=f"arXiv has no HTML endpoint for {arxiv_id}.",
                        result={"reason": "arxiv_html_404", "candidate": candidate.to_dict()},
                    )
                raise

            output_path = arxiv_service.write_html_file(
                attachment=attachment,
                source_pdf=Path(str(job.get("source_path") or attachment.file_path)),
                candidate=candidate,
                html_text=html_text,
            )
            relay_result: dict[str, Any] | None = None
            if self.config.arxiv_html_attach:
                if self.config.zotero_relay_url:
                    filename = arxiv_html_filename(attachment.filename)
                    existing = self._existing_html_sibling_by_filename(
                        attachment=attachment,
                        filename=filename,
                    )
                    if existing is not None:
                        relay_result = {
                            "ok": True,
                            "skipped": True,
                            "reason": "arxiv_html_sibling_exists",
                            "siblingKey": existing.key,
                            "filename": existing.filename,
                        }
                    else:
                        relay_result = self._create_html_sibling_via_relay(
                            attachment=attachment,
                            source_path=output_path,
                            filename=filename,
                            title=filename,
                            arxiv_id=arxiv_id,
                        )
                        self._write_html_sibling_local_copy(
                            attachment=attachment,
                            source_path=output_path,
                            filename=filename,
                            relay_result=relay_result,
                        )
                elif require_relay:
                    raise RuntimeError("ZOTERO_RELAY_URL is required before arXiv HTML can be attached.")

            result = {
                "attachment_key": attachment.key,
                "candidate": candidate.to_dict(),
                "html_path": str(output_path),
                "provider_events": list(arxiv_service.provider_events),
                "relay": relay_result,
            }
            return self.state.mark_metadata_job_succeeded(
                job_id=job_id,
                message=f"arXiv HTML saved for {arxiv_id}.",
                result=result,
                output_path=str(output_path),
                relay_result=relay_result,
            )
        except urllib.error.HTTPError as exc:
            return self.state.mark_metadata_job_failed(
                job_id=job_id,
                message=f"HTTP {exc.code} while fetching arXiv HTML: {_http_error_body(exc)}",
                retryable=exc.code in {408, 409, 425, 429, 500, 502, 503, 504},
            )
        except Exception as exc:
            return self.state.mark_metadata_job_failed(
                job_id=job_id,
                message=str(exc),
                retryable=not _is_nonretryable_worker_error(exc),
            )

    def _lookup_metadata_candidate(
        self,
        *,
        metadata: LocalItemMetadata,
        attachment: LocalAttachment,
    ) -> MetadataCandidate | None:
        enricher = self._metadata_enricher()
        candidate = enricher.lookup_candidate(metadata=metadata, attachment=attachment)
        self._provider_events = list(enricher.provider_events)
        return candidate

    def _metadata_enricher(self) -> MetadataEnricher:
        return MetadataEnricher(EnricherConfig(**metadata_enricher_config_kwargs(self.config)))

    def _full_text_output_dir(self, *, attachment: LocalAttachment, source_pdf: Path) -> Path:
        source = source_pdf if source_pdf.exists() else attachment.file_path
        signature = FileSignature.from_path(source)
        stem = _safe_filename(Path(attachment.filename).stem or "article")
        return (
            self.config.html_data_root
            / "source_discovery"
            / attachment.library_id
            / attachment.key
            / f"{signature.size}_{signature.mtime_ns}"
            / stem
        )

    def _full_text_output_dir_for_item(
        self,
        *,
        metadata: LocalItemMetadata,
        source_path: Path,
    ) -> Path:
        signature = FileSignature.from_path(source_path)
        stem = _safe_filename(metadata.title or metadata.key or "article")
        return (
            self.config.html_data_root
            / "source_discovery"
            / metadata.library_id
            / "items"
            / metadata.key
            / f"{signature.size}_{signature.mtime_ns}"
            / stem
        )

    def _full_text_context_for_job(
        self,
        job: dict[str, Any],
    ) -> tuple[LocalAttachment, LocalItemMetadata, dict[str, object], Path, str] | None:
        zotero = LocalZoteroStore(self._config_for_job(job))
        parent_key = str(job.get("parent_item_key") or "").strip()
        attachment_key = str(job.get("attachment_key") or "").strip()
        source_path = Path(str(job.get("source_path") or zotero.config.zotero_sqlite_path))

        if parent_key and attachment_key == parent_key:
            try:
                metadata = zotero.get_item_metadata(parent_key)
            except FileNotFoundError:
                return None
            attachment = self._synthetic_attachment_for_item(zotero=zotero, metadata=metadata)
            return (
                attachment,
                metadata,
                zotero.item_full_text_inventory(metadata),
                source_path if source_path.exists() else zotero.config.zotero_sqlite_path,
                "parent_item",
            )

        attachment = self._attachment_for_job(job)
        metadata = zotero.get_parent_metadata_for_attachment(attachment)
        if metadata is None:
            return None
        return (
            attachment,
            metadata,
            zotero.item_full_text_inventory(metadata),
            source_path if source_path.exists() else attachment.file_path,
            "attachment",
        )

    def _synthetic_attachment_for_item(
        self,
        *,
        zotero: LocalZoteroStore,
        metadata: LocalItemMetadata,
    ) -> LocalAttachment:
        filename = f"{_safe_filename(metadata.title or metadata.key)}.pdf"
        return LocalAttachment(
            library_id=metadata.library_id,
            data_dir=metadata.data_dir,
            storage_dir=zotero.config.resolved_storage_dir,
            key=metadata.key,
            item_id=None,
            parent_item_id=metadata.item_id,
            date_modified=metadata.date_modified,
            link_mode=None,
            content_type=None,
            zotero_path=None,
            file_path=Path(filename),
            parent_key=metadata.key,
        )

    def _patch_parent_metadata_via_relay(
        self,
        *,
        attachment: LocalAttachment,
        metadata: LocalItemMetadata,
        fields: dict[str, str],
        policy: str,
    ) -> dict[str, Any]:
        return ZoteroRelayClient(self.config).patch_parent_metadata(
            attachment=attachment,
            metadata=metadata,
            fields=fields,
            policy=policy,
            patch_digest=_patch_digest(fields),
        )

    def _create_html_sibling_via_relay(
        self,
        *,
        attachment: LocalAttachment,
        source_path: Path,
        filename: str,
        title: str,
        arxiv_id: str,
    ) -> dict[str, Any]:
        return ZoteroRelayClient(self.config).create_html_sibling(
            attachment=attachment,
            source_path=source_path,
            filename=filename,
            title=title,
            arxiv_id=arxiv_id,
        )

    def _attach_full_text_result(
        self,
        *,
        attachment: LocalAttachment,
        metadata: LocalItemMetadata,
        inventory: dict[str, object],
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        return FullTextAttachmentService(
            relay_enabled=bool(getattr(self.config, "zotero_relay_url", "")),
            create_parent_attachment=self._create_parent_attachment_via_relay,
            enqueue_pdf_for_ocr=self._enqueue_attached_pdf_for_ocr,
            enqueue_pdf_for_html=self._enqueue_attached_pdf_for_html,
            enqueue_html_for_translation=self._enqueue_attached_html_for_translation,
        ).attach(
            attachment=attachment,
            metadata=metadata,
            inventory=inventory,
            payload=payload,
        )

    def _create_parent_attachment_via_relay(
        self,
        *,
        metadata: LocalItemMetadata,
        attachment: LocalAttachment,
        source_path: Path,
        filename: str,
        title: str,
        content_type: str,
        probe_attachment_key: str | None,
        dedupe_prefix: str,
    ) -> dict[str, Any]:
        return ZoteroRelayClient(self.config).create_parent_attachment(
            metadata=metadata,
            source_path=source_path,
            filename=filename,
            title=title,
            content_type=content_type,
            probe_attachment_key=probe_attachment_key,
            dedupe_prefix=dedupe_prefix,
        )

    def _write_parent_attachment_local_copy(
        self,
        *,
        attachment: LocalAttachment,
        source_path: Path,
        filename: str,
        relay_result: dict[str, Any],
    ) -> dict[str, Any]:
        return write_parent_attachment_local_copy(
            attachment=attachment,
            source_path=source_path,
            filename=filename,
            relay_result=relay_result,
        )

    def _enqueue_attached_pdf_for_ocr(
        self,
        *,
        metadata: LocalItemMetadata,
        attachment: LocalAttachment,
        source_path: Path,
        relay_result: dict[str, Any],
    ) -> dict[str, Any]:
        return self._downstream_attachment_reference(
            stage="ocr",
            reason="full_text_pdf_needs_ocr",
            content_type="application/pdf",
            metadata=metadata,
            attachment=attachment,
            source_path=source_path,
            relay_result=relay_result,
        )

    def _enqueue_attached_pdf_for_html(
        self,
        *,
        metadata: LocalItemMetadata,
        attachment: LocalAttachment,
        source_path: Path,
        relay_result: dict[str, Any],
    ) -> dict[str, Any]:
        return self._downstream_attachment_reference(
            stage="pdf_html",
            reason="full_text_pdf_found",
            content_type="application/pdf",
            metadata=metadata,
            attachment=attachment,
            source_path=source_path,
            relay_result=relay_result,
        )

    def _skip_attached_pdf_for_html(
        self,
        *,
        metadata: LocalItemMetadata,
        attachment: LocalAttachment,
        source_path: Path,
        relay_result: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "skipped": True,
            "reason": "pdf_to_html_disabled_for_full_text_discovery",
            "source_path": str(source_path),
        }

    def _enqueue_attached_html_for_translation(
        self,
        *,
        metadata: LocalItemMetadata,
        attachment: LocalAttachment,
        source_path: Path,
        relay_result: dict[str, Any],
    ) -> dict[str, Any]:
        return self._downstream_attachment_reference(
            stage="translation",
            reason="full_text_html_found",
            content_type="text/html",
            metadata=metadata,
            attachment=attachment,
            source_path=source_path,
            relay_result=relay_result,
        )

    def _downstream_attachment_reference(
        self,
        *,
        stage: str,
        reason: str,
        content_type: str,
        metadata: LocalItemMetadata,
        attachment: LocalAttachment,
        source_path: Path,
        relay_result: dict[str, Any],
    ) -> dict[str, Any]:
        new_attachment = local_attachment_from_relay(
            metadata=metadata,
            attachment=attachment,
            source_path=source_path,
            relay_result=relay_result,
            content_type=content_type,
        )
        return {
            "ok": True,
            "skipped": True,
            "delegated": True,
            "classification": "downstream_orchestrator",
            "stage": stage,
            "reason": reason,
            "source_path": str(source_path),
            "attachment": new_attachment.to_dict(),
        }

    def _relay_json(
        self,
        *,
        method: str,
        path: str,
        payload: dict[str, Any],
        error_label: str,
    ) -> dict[str, Any]:
        return ZoteroRelayClient(self.config).request_json(
            method=method,
            path=path,
            payload=payload,
            error_label=error_label,
        )

    def _write_html_sibling_local_copy(
        self,
        *,
        attachment: LocalAttachment,
        source_path: Path,
        filename: str,
        relay_result: dict[str, Any],
    ) -> dict[str, Any]:
        sibling_key = str(
            relay_result.get("siblingKey")
            or relay_result.get("newAttachmentKey")
            or ""
        ).strip()
        if not sibling_key:
            raise RuntimeError("zotero-file-relay HTML sibling did not return siblingKey.")
        target_dir = attachment.storage_dir / sibling_key
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / Path(filename).name
        temp_path = target_dir / f".{target_path.name}.html-tmp"
        shutil.copy2(source_path, temp_path)
        os.replace(temp_path, target_path)
        return {"ok": True, "siblingKey": sibling_key, "path": str(target_path)}

    def _existing_html_sibling_by_filename(
        self,
        *,
        attachment: LocalAttachment,
        filename: str,
    ) -> LocalAttachment | None:
        if not attachment.parent_item_id:
            return None
        target = filename.casefold()
        zotero = LocalZoteroStore(self._config_for_attachment(attachment))
        for candidate in zotero.iter_html_attachments(max_items=100000):
            if candidate.parent_item_id != attachment.parent_item_id:
                continue
            if candidate.filename.casefold() == target:
                return candidate
        return None

    def _attachment_for_job(self, job: dict[str, Any]) -> LocalAttachment:
        zotero = LocalZoteroStore(self._config_for_job(job))
        attachment = zotero.get_attachment(str(job["attachment_key"]))
        source_path_raw = str(job.get("source_path") or "").strip()
        if not source_path_raw:
            return attachment
        source_path = Path(source_path_raw)
        if not source_path.exists():
            return attachment
        signature = FileSignature.from_path(source_path)
        if (
            signature.size == int(job["source_size"])
            and signature.mtime_ns == int(job["source_mtime_ns"])
        ):
            return dataclass_replace(attachment, file_path=source_path)
        return attachment

    def _config_for_job(self, job: dict[str, Any]) -> WorkerConfig:
        data_dir = Path(str(job.get("data_dir") or self.config.zotero_data_dir))
        for library_config in self._library_configs(
            library_id=str(job.get("library_id") or "") or None,
            data_dir=str(data_dir),
        ):
            return library_config
        return dataclass_replace(
            self.config,
            zotero_data_dir=data_dir,
            zotero_data_dirs=(data_dir,),
            zotero_storage_dir=None,
        )

    def _config_for_attachment(self, attachment: LocalAttachment) -> WorkerConfig:
        for library_config in self._library_configs(
            library_id=attachment.library_id,
            data_dir=str(attachment.data_dir),
        ):
            return library_config
        return dataclass_replace(
            self.config,
            zotero_data_dir=attachment.data_dir,
            zotero_data_dirs=(attachment.data_dir,),
            zotero_storage_dir=None,
        )

    def _library_configs(
        self,
        *,
        library_id: str | None = None,
        data_dir: str | None = None,
    ) -> list[WorkerConfig]:
        translated_data_dir = self.config.translate_zotero_input_path(data_dir) if data_dir else None
        configs: list[WorkerConfig] = []
        for candidate_data_dir in self.config.zotero_data_dirs:
            library_config = dataclass_replace(
                self.config,
                zotero_data_dir=candidate_data_dir,
                zotero_data_dirs=(candidate_data_dir,),
                zotero_storage_dir=None,
            )
            zotero = LocalZoteroStore(library_config)
            if library_id and zotero.library_id != library_id:
                continue
            if translated_data_dir and candidate_data_dir.resolve() != translated_data_dir.resolve():
                continue
            configs.append(library_config)
        return configs

    def _queue_key(self, job_type: str) -> str:
        return metadata_queue_key(self.config, job_type)


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
