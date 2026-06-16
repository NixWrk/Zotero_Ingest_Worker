from __future__ import annotations

import asyncio
import os
import shutil
import threading
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
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
)

from .arxiv_html import (
    ArxivHtmlJobService,
    arxiv_html_filename,
    parse_arxiv_atom,
    validate_arxiv_html,
)
from .config import WorkerConfig
from .metadata_backlog_scanner import (
    attachment_backlog_scan as scan_attachment_backlog,
    full_text_backlog_scan as scan_full_text_backlog,
    scihub_pdf_backlog_scan as scan_scihub_pdf_backlog,
)
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
)
from .local_zotero import LocalAttachment, LocalItemMetadata, LocalZoteroStore
from .local_attachment_sync import sync_ensured_parent_local, sync_parent_metadata_local
from .metadata_jobs import (
    METADATA_JOB_ARXIV_HTML,
    METADATA_JOB_ENRICH,
    METADATA_JOB_FULL_TEXT,
    METADATA_JOB_RESEARCHGATE_PDF,
    METADATA_JOB_SCIHUB_PDF,
    metadata_enricher_config_kwargs,
    metadata_queue_key,
)
from .metadata_processor_helpers import (
    build_metadata_diff,
    build_metadata_patch,
    crossref_work_to_candidate,
    extract_arxiv_id_from_text,
    extract_doi_from_text,
    filter_metadata_diff_for_item_type,
    first_full_text_output_path,
    full_text_worker_status,
    metadata_job_owner,
    normalize_arxiv_id,
    normalize_doi,
    title_match_score,
    zotero_translator_item_to_candidate,
    _doi_for_scihub,
    _encode_scihub_query_candidates,
    _enqueue_item_result,
    _enqueue_result,
    _first_researchgate_browser_fallback,
    _first_successful_pdf_download,
    _full_text_ocr_candidates,
    _http_error_body,
    _is_nonretryable_worker_error,
    _merge_extra,
    _metadata_haystack,
    _normalize_identifier,
    _patch_digest,
    _researchgate_result_retryable,
    _researchgate_url_from_job,
    _safe_filename,
    _scihub_doi_from_job,
    _scihub_query_candidates,
    _scihub_query_from_job,
    _scihub_queries_from_job,
    _scihub_query_type_from_job,
    _scihub_result_retryable,
    _title_for_lookup,
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
        return scan_attachment_backlog(
            self,
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
        return scan_attachment_backlog(
            self,
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
        return scan_full_text_backlog(
            self,
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
        return scan_scihub_pdf_backlog(
            self,
            max_items=max_items,
            limit=limit,
            force=force,
            library_id=library_id,
            data_dir=data_dir,
            collection=collection,
        )

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

        enqueue = self._enqueue_scihub_pdf_query_job(
            metadata=metadata,
            candidates=candidates,
            reason=reason,
            force=force,
        )
        jobs = [enqueue] if enqueue is not None else []
        queued = 1 if enqueue is not None and enqueue.get("classification") == "queued" else 0

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
        candidates: list[dict[str, str]],
        reason: str,
        force: bool,
    ) -> dict[str, Any] | None:
        candidates = [
            {
                "type": _normalize_identifier(str(candidate.get("type") or "")),
                "query": _normalize_identifier(str(candidate.get("query") or "")),
            }
            for candidate in candidates
        ]
        candidates = [candidate for candidate in candidates if candidate["type"] and candidate["query"]]
        if not candidates:
            return None

        source_path = Path(metadata.data_dir) / "zotero.sqlite"
        signature = FileSignature.from_path(source_path)
        queue_key = (
            f"{self._queue_key(METADATA_JOB_SCIHUB_PDF)}"
            f"|query_list={_encode_scihub_query_candidates(candidates)}"
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
                    "queries": candidates,
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
            "queries": candidates,
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
        workers = self._drain_worker_count(limit=effective_limit)

        if workers == 1:
            results = self._drain_leased_jobs_sequential(
                job_type=job_type,
                limit=effective_limit,
                owner=owner,
                lease_seconds=lease_seconds,
                require_relay=require_relay,
                policy=policy,
            )
        else:
            results = self._drain_leased_jobs_parallel(
                job_type=job_type,
                limit=effective_limit,
                workers=workers,
                owner=owner,
                lease_seconds=lease_seconds,
                require_relay=require_relay,
                policy=policy,
            )

        processed = len(results)
        failed = sum(
            1
            for result in results
            if result.get("status") in {"failed_retryable", "failed_final"}
        )

        return {
            "ok": failed == 0,
            "mode": f"{job_type}_drain_queue",
            "job_type": job_type,
            "processed": processed,
            "failed": failed,
            "recovered": recovered,
            "workers": workers,
            "queue": self.state.metadata_queue_summary(job_type=job_type),
            "results": results,
        }

    def _drain_worker_count(self, *, limit: int) -> int:
        max_workers = max(int(getattr(self.config, "metadata_drain_max_workers", 1)), 1)
        if limit <= 1:
            return 1
        return min(limit, max_workers)

    def _drain_leased_jobs_sequential(
        self,
        *,
        job_type: str,
        limit: int,
        owner: str,
        lease_seconds: int,
        require_relay: bool,
        policy: str | None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        while len(results) < limit:
            job = self.state.lease_next_metadata_job(
                job_type=job_type,
                owner=owner,
                lease_seconds=lease_seconds,
            )
            if job is None:
                break
            results.append(
                self._drain_leased_job(
                    job_type=job_type,
                    job=job,
                    require_relay=require_relay,
                    policy=policy,
                )
            )
        return results

    def _drain_leased_jobs_parallel(
        self,
        *,
        job_type: str,
        limit: int,
        workers: int,
        owner: str,
        lease_seconds: int,
        require_relay: bool,
        policy: str | None,
    ) -> list[dict[str, Any]]:
        counter_lock = threading.Lock()
        leased_slots = 0

        def worker(worker_index: int) -> list[dict[str, Any]]:
            nonlocal leased_slots
            processor = self._new_drain_worker_processor()
            local_results: list[dict[str, Any]] = []
            worker_owner = f"{owner}-{worker_index + 1}"
            while True:
                with counter_lock:
                    if leased_slots >= limit:
                        return local_results
                    leased_slots += 1
                job = processor.state.lease_next_metadata_job(
                    job_type=job_type,
                    owner=worker_owner,
                    lease_seconds=lease_seconds,
                )
                if job is None:
                    return local_results
                local_results.append(
                    processor._drain_leased_job(
                        job_type=job_type,
                        job=job,
                        require_relay=require_relay,
                        policy=policy,
                    )
                )

        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(worker, index) for index in range(workers)]
            for future in as_completed(futures):
                results.extend(future.result())
        return results

    def _new_drain_worker_processor(self) -> "ZoteroMetadataProcessor":
        return type(self)(self.config)

    def _drain_leased_job(
        self,
        *,
        job_type: str,
        job: dict[str, Any],
        require_relay: bool,
        policy: str | None,
    ) -> dict[str, Any]:
        if job_type == METADATA_JOB_ENRICH:
            return self._drain_enrich_job(
                job,
                require_relay=require_relay,
                policy=policy or self.config.metadata_policy,
            )
        if job_type == METADATA_JOB_ARXIV_HTML:
            return self._drain_arxiv_html_job(job, require_relay=require_relay)
        if job_type == METADATA_JOB_FULL_TEXT:
            return self._drain_full_text_job(job)
        if job_type == METADATA_JOB_RESEARCHGATE_PDF:
            return self._drain_researchgate_pdf_job(job)
        if job_type == METADATA_JOB_SCIHUB_PDF:
            return self._drain_scihub_pdf_job(job)
        return self.state.mark_metadata_job_failed(
            job_id=str(job["job_id"]),
            message=f"Unknown metadata job type: {job_type}",
            retryable=False,
        )

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
            attachment, metadata, parent_preflight = self._ensure_parent_metadata_context(
                zotero=zotero,
                attachment=attachment,
            )
            if metadata is None:
                return self.state.mark_metadata_job_skipped(
                    job_id=job_id,
                    message="PDF attachment has no parent item to patch.",
                    result={
                        "reason": "no_parent_item",
                        "attachment": attachment.to_dict(),
                        "parent_preflight": parent_preflight,
                    },
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
                        "parent_preflight": parent_preflight,
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
                        "parent_preflight": parent_preflight,
                        "provider_events": list(self._provider_events),
                    },
                )
            if metadata.version is None and not getattr(self.config, "zotero_relay_url", ""):
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
                if metadata.item_id <= 0:
                    local_metadata = {
                        "ok": True,
                        "updated": False,
                        "reason": "parent_not_in_local_sqlite",
                        "item_key": metadata.key,
                    }
                else:
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
                "parent_preflight": parent_preflight,
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
            # Each job may carry several DOI/PMID/PMCID/URL candidates. They
            # are tried sequentially inside one parent-scoped job so the queue
            # does not fan out into duplicate fallback tasks.
            queries = _scihub_queries_from_job(job)
            if not queries:
                queries = [{"type": _scihub_query_type_from_job(job), "query": _scihub_query_from_job(job)}]
            item_key = str(job.get("parent_item_key") or job.get("attachment_key") or "").strip()
            if not item_key:
                return self.state.mark_metadata_job_skipped(
                    job_id=job_id,
                    message="Sci-Hub PDF job has no parent item key.",
                    result={
                        "reason": "missing_parent_item_key",
                        "queries": queries,
                        "job": job,
                    },
                )

            attempts: list[dict[str, Any]] = []
            for candidate in queries:
                query = _normalize_identifier(str(candidate.get("query") or ""))
                query_type = _normalize_identifier(str(candidate.get("type") or "")) or "doi"
                # The query may be a DOI, PMID, PMCID, URL, or other identifier.
                # When it is absent the adapter derives the best DOI from metadata.
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
                attempts.append(dict(result))
                if result.get("ok"):
                    success_result = dict(result)
                    success_result["attempts"] = attempts
                    download = result.get("download")
                    output_path = None
                    if isinstance(download, dict):
                        output_path = str(download.get("output_path") or "").strip() or None
                    return self.state.mark_metadata_job_succeeded(
                        job_id=job_id,
                        message=f"Sci-Hub PDF job finished with status {result.get('status')}.",
                        result=success_result,
                        output_path=output_path,
                        relay_result=success_result.get("attach")
                        if isinstance(success_result.get("attach"), dict)
                        else None,
                    )

            result = dict(attempts[-1]) if attempts else {"ok": False, "status": "missing_query"}
            result["ok"] = False
            result["attempts"] = attempts
            return self.state.mark_metadata_job_failed(
                job_id=job_id,
                message=str(result.get("error") or result.get("status") or "Sci-Hub PDF download failed."),
                retryable=any(_scihub_result_retryable(attempt) for attempt in attempts),
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
        attachment, metadata, _parent_preflight = self._ensure_parent_metadata_context(
            zotero=zotero,
            attachment=attachment,
        )
        if metadata is None:
            return None
        inventory = (
            zotero.item_full_text_inventory(metadata)
            if metadata.item_id > 0
            else self._synthetic_pdf_inventory_for_attachment(attachment)
        )
        return (
            attachment,
            metadata,
            inventory,
            source_path if source_path.exists() else attachment.file_path,
            "attachment",
        )

    def _ensure_parent_metadata_context(
        self,
        *,
        zotero: LocalZoteroStore,
        attachment: LocalAttachment,
    ) -> tuple[LocalAttachment, LocalItemMetadata | None, dict[str, Any] | None]:
        metadata = zotero.get_parent_metadata_for_attachment(attachment)
        if metadata is not None:
            return attachment, metadata, None
        if not getattr(self.config, "zotero_parent_preflight_enabled", True):
            return attachment, None, {"ok": True, "skipped": True, "reason": "disabled"}
        if not getattr(self.config, "zotero_relay_url", ""):
            return attachment, None, {"ok": True, "skipped": True, "reason": "relay_not_configured"}

        parent_preflight = self._ensure_parent_via_relay(attachment)
        parent_key = str(parent_preflight.get("parentItemKey") or "").strip()
        if not parent_key:
            return attachment, None, parent_preflight
        parent_preflight = self._sync_ensured_parent_locally(
            attachment=attachment,
            parent_preflight=parent_preflight,
        )
        attachment = dataclass_replace(attachment, parent_key=parent_key)
        local_sync = parent_preflight.get("local_sync")
        if isinstance(local_sync, dict) and local_sync.get("parentItemId"):
            attachment = dataclass_replace(
                attachment,
                parent_item_id=int(local_sync["parentItemId"]),
            )
        metadata = zotero.get_parent_metadata_for_attachment(attachment)
        if metadata is not None:
            return attachment, metadata, parent_preflight
        return (
            attachment,
            self._synthetic_parent_metadata_from_preflight(
                attachment=attachment,
                parent_key=parent_key,
                parent_preflight=parent_preflight,
            ),
            parent_preflight,
        )

    def _ensure_parent_via_relay(self, attachment: LocalAttachment) -> dict[str, Any]:
        return ZoteroRelayClient(self.config).ensure_parent(attachment)

    def _sync_ensured_parent_locally(
        self,
        *,
        attachment: LocalAttachment,
        parent_preflight: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            local_sync = sync_ensured_parent_local(
                attachment=attachment,
                relay_result=parent_preflight,
            )
        except Exception as exc:
            local_sync = {
                "ok": False,
                "reason": "local_parent_sync_failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        return {**parent_preflight, "local_sync": local_sync}

    def _synthetic_parent_metadata_from_preflight(
        self,
        *,
        attachment: LocalAttachment,
        parent_key: str,
        parent_preflight: dict[str, Any],
    ) -> LocalItemMetadata:
        parent_created = parent_preflight.get("parentCreated")
        parent_payload = parent_created if isinstance(parent_created, dict) else {}
        title = str(parent_payload.get("title") or Path(attachment.filename).stem or "Untitled PDF")
        return LocalItemMetadata(
            library_id=attachment.library_id,
            data_dir=attachment.data_dir,
            key=parent_key,
            item_id=0,
            version=_optional_int(parent_payload.get("version")),
            item_type=str(parent_payload.get("itemType") or "document"),
            date_modified=None,
            fields={"title": title},
            creators=[],
            tags=[],
            collections=[],
            relations=[],
        )

    def _synthetic_pdf_inventory_for_attachment(
        self,
        attachment: LocalAttachment,
    ) -> dict[str, object]:
        exists = _safe_path_exists(attachment.file_path)
        return {
            "pdf_count": 1,
            "html_count": 0,
            "source_html_count": 0,
            "generated_html_count": 0,
            "unknown_html_count": 0,
            "missing_file_count": 0 if exists else 1,
            "has_pdf": True,
            "has_html": False,
            "has_source_html": False,
            "attachments": [
                {
                    "key": attachment.key,
                    "content_type": attachment.content_type or "application/pdf",
                    "path": attachment.zotero_path or str(attachment.file_path),
                    "title": attachment.filename,
                    "file_path": str(attachment.file_path),
                    "exists": exists,
                    "is_pdf": True,
                    "is_html": False,
                    "is_source_html": False,
                    "is_generated_html": False,
                }
            ],
        }

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


def _optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None


def _safe_path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False
