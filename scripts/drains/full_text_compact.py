from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import replace as dataclass_replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from zotero_ingest_worker.config import from_env
from zotero_ingest_worker.full_text_discovery import summarize_full_text_payload
from zotero_ingest_worker.metadata_jobs import (
    METADATA_JOB_FULL_TEXT,
    METADATA_JOB_RESEARCHGATE_PDF,
    METADATA_JOB_SCIHUB_PDF,
)
from zotero_ingest_worker.metadata_processor import ZoteroMetadataProcessor


FULL_TEXT_PDF_CYCLE = "full_text_pdf_cycle"
SCIHUB_PDF_BACKLOG_SCAN = "scihub_pdf_backlog_scan"
SUPPORTED_JOB_TYPES = (
    METADATA_JOB_FULL_TEXT,
    METADATA_JOB_RESEARCHGATE_PDF,
    METADATA_JOB_SCIHUB_PDF,
)
SUPPORTED_RUN_TYPES = (*SUPPORTED_JOB_TYPES, FULL_TEXT_PDF_CYCLE)
PDF_FALLBACK_CYCLE = (
    METADATA_JOB_FULL_TEXT,
    METADATA_JOB_RESEARCHGATE_PDF,
    SCIHUB_PDF_BACKLOG_SCAN,
    METADATA_JOB_SCIHUB_PDF,
)


def _load_payload(job: dict[str, Any]) -> dict[str, Any]:
    raw = job.get("result_json")
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"payload_error": "invalid_result_json"}
    return payload if isinstance(payload, dict) else {}


def summarize_job(job: dict[str, Any]) -> dict[str, Any]:
    payload = _load_payload(job)
    relay_attachment = payload.get("relay_attachment")
    if not isinstance(relay_attachment, dict):
        relay_attachment = {}
    relay = relay_attachment.get("relay")
    if not isinstance(relay, dict):
        relay = {}
    pdf_attachment = relay_attachment.get("pdf_attachment")
    if not isinstance(pdf_attachment, dict):
        pdf_attachment = {}
    pdf_relay = pdf_attachment.get("relay")
    if not isinstance(pdf_relay, dict):
        pdf_relay = {}
    translation_enqueue = relay_attachment.get("translation_enqueue")
    if not isinstance(translation_enqueue, dict):
        translation_enqueue = {}
    translation_job = translation_enqueue.get("job")
    if not isinstance(translation_job, dict):
        translation_job = {}

    full_text_summary = summarize_full_text_payload(payload)

    existing_pdf_enqueue = payload.get("existing_pdf_enqueue")
    if not isinstance(existing_pdf_enqueue, dict):
        existing_pdf_enqueue = {}
    existing_html_enqueue = existing_pdf_enqueue.get("html_enqueue")
    if not isinstance(existing_html_enqueue, dict):
        existing_html_enqueue = {}
    existing_ocr_enqueue = existing_pdf_enqueue.get("ocr_enqueue")
    if not isinstance(existing_ocr_enqueue, dict):
        existing_ocr_enqueue = {}
    existing_html_job = existing_html_enqueue.get("job")
    if not isinstance(existing_html_job, dict):
        existing_html_job = {}
    existing_ocr_job = existing_ocr_enqueue.get("job")
    if not isinstance(existing_ocr_job, dict):
        existing_ocr_job = {}

    return {
        "job_id": job.get("job_id"),
        "parent_item_key": job.get("parent_item_key"),
        "attachment_key": job.get("attachment_key"),
        "status": job.get("status"),
        "full_text_status": payload.get("worker_status")
        or full_text_summary.worker_status,
        "relay_kind": relay_attachment.get("kind"),
        "attached_kinds": relay_attachment.get("attached_kinds"),
        "new_attachment_key": relay.get("newAttachmentKey")
        or relay.get("attachmentKey"),
        "pdf_new_attachment_key": pdf_relay.get("newAttachmentKey")
        or pdf_relay.get("attachmentKey"),
        "translation_job_id": translation_job.get("job_id"),
        "translation_status": translation_enqueue.get("classification"),
        "html_found": bool(full_text_summary.accepted_html),
        "html_rejected": bool(full_text_summary.rejected_html),
        "pdf_found": bool(full_text_summary.successful_pdf),
        "browser_pdf_fallbacks": len(full_text_summary.browser_fallbacks),
        "existing_pdf_html_job_id": existing_html_job.get("job_id"),
        "existing_pdf_html_status": existing_html_enqueue.get("classification"),
        "existing_pdf_ocr_job_id": existing_ocr_job.get("job_id"),
        "existing_pdf_ocr_status": existing_ocr_enqueue.get("classification"),
        "output_path": payload.get("output_path")
        or full_text_summary.output_path
        or job.get("output_path"),
        "error": job.get("last_error") or payload.get("error"),
    }


def summarize_scihub_pdf_job(job: dict[str, Any]) -> dict[str, Any]:
    payload = _load_payload(job)
    download = payload.get("download")
    if not isinstance(download, dict):
        download = {}
    attach = payload.get("attach")
    if not isinstance(attach, dict):
        attach = {}
    relay = attach.get("relay")
    if not isinstance(relay, dict):
        relay = {}

    scihub_status = str(payload.get("status") or download.get("status") or "").strip()
    attached = payload.get("ok") is True and scihub_status == "attached"
    already_has_pdf = scihub_status == "parent_already_has_pdf"
    skipped = download.get("skipped")
    downloaded = download.get("ok") is True and (skipped is None or skipped is False)

    return {
        "job_id": job.get("job_id"),
        "parent_item_key": job.get("parent_item_key"),
        "attachment_key": job.get("attachment_key"),
        "status": job.get("status"),
        "scihub_status": scihub_status or None,
        "query": payload.get("query") or download.get("query"),
        "query_type": payload.get("query_type") or download.get("query_type"),
        "doi": download.get("doi") or payload.get("doi"),
        "scihub_url": download.get("scihub_url") or payload.get("scihub_url"),
        "pdf_url": download.get("pdf_url") or payload.get("pdf_url"),
        "output_path": download.get("output_path")
        or payload.get("output_path")
        or job.get("output_path"),
        "new_attachment_key": relay.get("newAttachmentKey")
        or relay.get("attachmentKey"),
        "pdf_found": attached,
        "scihub_attached": attached,
        "scihub_downloaded": downloaded,
        "already_has_pdf": already_has_pdf,
        "error": job.get("last_error") or payload.get("error") or download.get("error"),
    }


def summarize_researchgate_pdf_job(job: dict[str, Any]) -> dict[str, Any]:
    payload = _load_payload(job)
    download = payload.get("download")
    if not isinstance(download, dict):
        download = {}
    attach = payload.get("attach")
    if not isinstance(attach, dict):
        attach = {}
    relay = attach.get("relay")
    if not isinstance(relay, dict):
        relay = {}

    researchgate_status = str(
        payload.get("status") or download.get("status") or ""
    ).strip()
    attached = payload.get("ok") is True and researchgate_status == "attached"
    already_has_pdf = researchgate_status == "parent_already_has_pdf"
    skipped = download.get("skipped")
    downloaded = download.get("ok") is True and (skipped is None or skipped is False)

    return {
        "job_id": job.get("job_id"),
        "parent_item_key": job.get("parent_item_key"),
        "attachment_key": job.get("attachment_key"),
        "status": job.get("status"),
        "researchgate_status": researchgate_status or None,
        "researchgate_url": download.get("url")
        or download.get("source_url")
        or payload.get("url"),
        "output_path": download.get("output_path")
        or payload.get("output_path")
        or job.get("output_path"),
        "new_attachment_key": relay.get("newAttachmentKey")
        or relay.get("attachmentKey"),
        "pdf_found": attached,
        "researchgate_attached": attached,
        "researchgate_downloaded": downloaded,
        "already_has_pdf": already_has_pdf,
        "error": job.get("last_error") or payload.get("error") or download.get("error"),
    }


def _nonnegative_batch_count(value: object, *, field: str) -> int:
    if type(value) is not int or value < 0:
        raise RuntimeError(f"batch {field} must be an exact non-negative integer")
    return value


def _print_json(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=True), flush=True)


def compact_batch(
    result: dict[str, Any], *, job_type: str = METADATA_JOB_FULL_TEXT
) -> dict[str, Any]:
    raw_jobs = result.get("results", [])
    if not isinstance(raw_jobs, list):
        raise RuntimeError("batch results must be an array")
    jobs: list[dict[str, Any]] = []
    for index, job in enumerate(raw_jobs):
        if not isinstance(job, dict):
            raise RuntimeError(f"batch results[{index}] must be an object")
        jobs.append(job)
    if job_type == METADATA_JOB_SCIHUB_PDF:
        summarized = [summarize_scihub_pdf_job(job) for job in jobs]
    elif job_type == METADATA_JOB_RESEARCHGATE_PDF:
        summarized = [summarize_researchgate_pdf_job(job) for job in jobs]
    else:
        summarized = [summarize_job(job) for job in jobs]
    queue = result.get("queue", {})
    if not isinstance(queue, dict):
        raise RuntimeError("batch queue must be an object")
    processed = _nonnegative_batch_count(result.get("processed", 0), field="processed")
    failed = _nonnegative_batch_count(result.get("failed", 0), field="failed")
    if failed > processed:
        raise RuntimeError("batch failed count cannot exceed processed count")
    ok = result.get("ok")
    if type(ok) is not bool:
        raise RuntimeError("batch ok must be an exact boolean")

    if len(jobs) != processed:
        raise RuntimeError("batch results count must equal processed count")
    failed_retryable_jobs = sum(
        1 for item in summarized if item.get("status") == "failed_retryable"
    )
    failed_final_jobs = sum(
        1 for item in summarized if item.get("status") == "failed_final"
    )
    if failed_retryable_jobs + failed_final_jobs != failed:
        raise RuntimeError("batch failed count must match failed job statuses")

    batch_error = not ok and failed == 0
    compact = {
        "at": datetime.now(timezone.utc).isoformat(),
        "job_type": job_type,
        "ok": ok,
        "batch_error": batch_error,
        "processed": processed,
        "failed": failed,
        "failed_retryable_jobs": failed_retryable_jobs,
        "failed_final_jobs": failed_final_jobs,
        "queue": {
            "queued": queue.get("queued"),
            "running": queue.get("running"),
            "succeeded": queue.get("succeeded"),
            "failed_retryable": queue.get("failed_retryable"),
            "failed_final": queue.get("failed_final"),
        },
        "html_found": sum(1 for item in summarized if item.get("html_found")),
        "html_rejected": sum(1 for item in summarized if item.get("html_rejected")),
        "pdf_found": sum(1 for item in summarized if item.get("pdf_found")),
        "browser_pdf_fallbacks": sum(
            int(item.get("browser_pdf_fallbacks") or 0) for item in summarized
        ),
        "existing_pdf_html_queued": sum(
            1 for item in summarized if item.get("existing_pdf_html_job_id")
        ),
        "existing_pdf_ocr_queued": sum(
            1 for item in summarized if item.get("existing_pdf_ocr_job_id")
        ),
        "translation_queued": sum(
            1 for item in summarized if item.get("translation_job_id")
        ),
        "jobs": summarized,
    }
    if job_type == METADATA_JOB_SCIHUB_PDF:
        compact["scihub_attached"] = sum(
            1 for item in summarized if item.get("scihub_attached")
        )
        compact["scihub_downloaded"] = sum(
            1 for item in summarized if item.get("scihub_downloaded")
        )
        compact["already_has_pdf"] = sum(
            1 for item in summarized if item.get("already_has_pdf")
        )
    if job_type == METADATA_JOB_RESEARCHGATE_PDF:
        compact["researchgate_attached"] = sum(
            1 for item in summarized if item.get("researchgate_attached")
        )
        compact["researchgate_downloaded"] = sum(
            1 for item in summarized if item.get("researchgate_downloaded")
        )
        compact["already_has_pdf"] = sum(
            1 for item in summarized if item.get("already_has_pdf")
        )
    return compact


class ParallelDrainCoordinator:
    def __init__(
        self,
        *,
        log_path: Path,
        limit: int | None,
        batch_size: int,
        max_failures: int,
        max_retryable_failures: int,
    ) -> None:
        self.log_path = log_path
        self.limit = limit
        self.batch_size = batch_size
        self.max_failures = max_failures
        self.max_retryable_failures = max_retryable_failures
        self._reserved = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.total_processed = 0
        self.total_failed = 0
        self.total_failed_retryable = 0
        self.total_failed_final = 0
        self.total_html_found = 0
        self.total_batch_errors = 0
        self.total_pdf_found = 0
        self.total_translation_queued = 0
        self.total_researchgate_attached = 0
        self.total_researchgate_downloaded = 0
        self.total_scihub_attached = 0
        self.total_scihub_downloaded = 0
        self.total_already_has_pdf = 0

    def reserve_batch(self, max_batch: int | None = None) -> int:
        with self._lock:
            if self._stop.is_set():
                return 0
            batch_size = self.batch_size
            if max_batch is not None:
                batch_size = min(batch_size, max(1, int(max_batch)))
            if self.limit is None:
                return batch_size
            remaining = self.limit - self._reserved
            if remaining <= 0:
                return 0
            reserved = min(batch_size, remaining)
            self._reserved += reserved
            return reserved

    def complete_batch(self, *, reserved: int, processed: int) -> None:
        if type(reserved) is not int or reserved <= 0:
            raise RuntimeError("reserved batch size must be a positive integer")
        if type(processed) is not int or not 0 <= processed <= reserved:
            raise RuntimeError(
                "processed batch size must be a non-negative integer no greater than reserved"
            )
        if self.limit is None:
            return
        unused = reserved - processed
        if unused == 0:
            return
        with self._lock:
            if unused > self._reserved:
                raise RuntimeError(
                    "cannot release more drain capacity than is reserved"
                )
            self._reserved -= unused

    def limit_reached(self) -> bool:
        with self._lock:
            return self.limit is not None and self._reserved >= self.limit

    def stop(self) -> None:
        self._stop.set()

    def should_stop(self) -> bool:
        return self._stop.is_set()

    def record(self, compact: dict[str, Any]) -> None:
        with self._lock:
            self.total_processed += int(compact.get("processed") or 0)
            self.total_failed += int(compact.get("failed") or 0)
            self.total_failed_retryable += int(
                compact.get("failed_retryable_jobs") or 0
            )
            self.total_failed_final += int(compact.get("failed_final_jobs") or 0)
            self.total_html_found += int(compact.get("html_found") or 0)
            self.total_batch_errors += int(compact.get("batch_error") is True)
            self.total_pdf_found += int(compact.get("pdf_found") or 0)
            self.total_translation_queued += int(compact.get("translation_queued") or 0)
            self.total_researchgate_attached += int(
                compact.get("researchgate_attached") or 0
            )
            self.total_researchgate_downloaded += int(
                compact.get("researchgate_downloaded") or 0
            )
            self.total_scihub_attached += int(compact.get("scihub_attached") or 0)
            self.total_scihub_downloaded += int(compact.get("scihub_downloaded") or 0)
            self.total_already_has_pdf += int(compact.get("already_has_pdf") or 0)

            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(compact, ensure_ascii=False) + "\n")
            _print_json(compact)

            if self.max_failures > 0 and self.total_failed >= self.max_failures:
                self._stop.set()
            if (
                self.max_retryable_failures > 0
                and self.total_failed_retryable >= self.max_retryable_failures
            ):
                self._stop.set()
            if self.total_batch_errors > 0:
                self._stop.set()


def _job_label(job_type: str) -> str:
    return job_type.replace("_", "-")


def _job_exit_code(*, failed: int, batch_errors: int) -> int:
    return 1 if failed or batch_errors else 0


def _drain_metadata_batch(
    processor: ZoteroMetadataProcessor,
    *,
    job_type: str,
    limit: int,
    dry_run: bool,
) -> dict[str, Any]:
    if job_type == METADATA_JOB_SCIHUB_PDF:
        return processor.drain_scihub_pdf_queue(limit=limit, dry_run=dry_run)
    if job_type == METADATA_JOB_RESEARCHGATE_PDF:
        return processor.drain_researchgate_pdf_queue(limit=limit, dry_run=dry_run)
    return processor.drain_full_text_queue(limit=limit, dry_run=dry_run)


def _single_worker_processor() -> ZoteroMetadataProcessor:
    config = dataclass_replace(from_env(), metadata_drain_max_workers=1)
    return ZoteroMetadataProcessor(config)


def _drain_parallel_worker(
    *,
    worker_index: int,
    args: argparse.Namespace,
    coordinator: ParallelDrainCoordinator,
) -> dict[str, Any]:
    if args.worker_stagger_seconds > 0 and worker_index > 1:
        time.sleep(args.worker_stagger_seconds * (worker_index - 1))

    processor = _single_worker_processor()
    local_processed = 0
    local_failed = 0

    while not coordinator.should_stop():
        batch_limit = coordinator.reserve_batch()
        if batch_limit <= 0:
            break

        try:
            result = _drain_metadata_batch(
                processor,
                job_type=args.job_type,
                limit=batch_limit,
                dry_run=args.dry_run,
            )
            compact = compact_batch(result, job_type=args.job_type)
        except BaseException:
            coordinator.complete_batch(reserved=batch_limit, processed=0)
            raise
        compact["worker_index"] = worker_index
        compact["worker_name"] = f"{_job_label(args.job_type)}-worker-{worker_index}"

        processed = int(compact.get("processed") or 0)
        failed = int(compact.get("failed") or 0)
        coordinator.complete_batch(reserved=batch_limit, processed=processed)
        local_processed += processed
        local_failed += failed
        coordinator.record(compact)

        if processed == 0 or args.dry_run:
            break
        if args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)

    return {
        "worker_index": worker_index,
        "processed": local_processed,
        "failed": local_failed,
    }


def _run_parallel(args: argparse.Namespace) -> int:
    log_path = Path(args.log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    limit = None if args.limit <= 0 else args.limit
    workers = max(1, int(args.workers))

    coordinator = ParallelDrainCoordinator(
        log_path=log_path,
        limit=limit,
        batch_size=max(1, int(args.batch_size)),
        max_failures=max(0, int(args.max_failures)),
        max_retryable_failures=max(0, int(args.max_retryable_failures)),
    )
    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix=f"{_job_label(args.job_type)}-drain",
    ) as executor:
        futures = [
            executor.submit(
                _drain_parallel_worker,
                worker_index=index + 1,
                args=args,
                coordinator=coordinator,
            )
            for index in range(workers)
        ]
        worker_summaries = [future.result() for future in futures]

    summary = {
        "at": datetime.now(timezone.utc).isoformat(),
        "done": True,
        "job_type": args.job_type,
        "dry_run": bool(args.dry_run),
        "workers": workers,
        "processed": coordinator.total_processed,
        "failed": coordinator.total_failed,
        "failed_retryable_jobs": coordinator.total_failed_retryable,
        "failed_final_jobs": coordinator.total_failed_final,
        "html_found": coordinator.total_html_found,
        "batch_errors": coordinator.total_batch_errors,
        "pdf_found": coordinator.total_pdf_found,
        "researchgate_attached": coordinator.total_researchgate_attached,
        "researchgate_downloaded": coordinator.total_researchgate_downloaded,
        "scihub_attached": coordinator.total_scihub_attached,
        "scihub_downloaded": coordinator.total_scihub_downloaded,
        "already_has_pdf": coordinator.total_already_has_pdf,
        "translation_queued": coordinator.total_translation_queued,
        "worker_summaries": worker_summaries,
        "log_path": str(log_path),
    }
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False) + "\n")
    _print_json(summary)
    return _job_exit_code(
        failed=coordinator.total_failed,
        batch_errors=coordinator.total_batch_errors,
    )


def _metadata_queue_summary(
    *,
    job_type: str,
    lease_owner: str | None = None,
) -> dict[str, Any]:
    processor = ZoteroMetadataProcessor(from_env())
    recover_expired = getattr(processor.state, "recover_expired_metadata_jobs", None)
    if callable(recover_expired):
        recover_expired(job_type=job_type)
    queue = processor.state.metadata_queue_summary(job_type=job_type)
    if lease_owner:
        list_jobs = getattr(processor.state, "list_metadata_jobs", None)
        if callable(list_jobs):
            running_jobs = list_jobs(
                job_type=job_type,
                statuses={"running"},
                limit=None,
            )
            queue["owned_running"] = sum(
                1 for job in running_jobs if job.get("lease_owner") == lease_owner
            )
    return queue


def _full_text_queue_summary(*, lease_owner: str | None = None) -> dict[str, Any]:
    return _metadata_queue_summary(
        job_type=METADATA_JOB_FULL_TEXT, lease_owner=lease_owner
    )


def _drain_dynamic_batch(
    *,
    worker_index: int,
    args: argparse.Namespace,
    batch_limit: int,
) -> dict[str, Any]:
    processor = _single_worker_processor()
    result = _drain_metadata_batch(
        processor,
        job_type=args.job_type,
        limit=batch_limit,
        dry_run=False,
    )
    compact = compact_batch(result, job_type=args.job_type)
    compact["worker_index"] = worker_index
    compact["worker_name"] = f"{_job_label(args.job_type)}-dynamic-{worker_index}"
    compact["dynamic"] = True
    return compact


def _run_dynamic_parallel(args: argparse.Namespace) -> int:
    log_path = Path(args.log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    limit = None if args.limit <= 0 else args.limit
    max_workers = max(1, int(args.workers))
    poll_seconds = max(0.1, float(args.dynamic_poll_seconds))

    coordinator = ParallelDrainCoordinator(
        log_path=log_path,
        limit=limit,
        batch_size=max(1, int(args.batch_size)),
        max_failures=max(0, int(args.max_failures)),
        max_retryable_failures=max(0, int(args.max_retryable_failures)),
    )
    inflight: dict[Future[dict[str, Any]], tuple[int, int]] = {}
    worker_summaries: dict[int, dict[str, int]] = {}
    worker_sequence = 0

    def record_completed(done: set[Future[dict[str, Any]]]) -> None:
        for future in done:
            worker_index, batch_limit = inflight.pop(future)
            try:
                compact = future.result()
            except BaseException:
                coordinator.complete_batch(reserved=batch_limit, processed=0)
                raise
            processed = int(compact.get("processed") or 0)
            failed = int(compact.get("failed") or 0)
            coordinator.complete_batch(reserved=batch_limit, processed=processed)
            coordinator.record(compact)

            worker_summary = worker_summaries.setdefault(
                worker_index,
                {"worker_index": worker_index, "processed": 0, "failed": 0},
            )
            worker_summary["processed"] += processed
            worker_summary["failed"] += failed

    with ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix=f"{_job_label(args.job_type)}-dynamic",
    ) as executor:
        while not coordinator.should_stop():
            finished_now = {future for future in inflight if future.done()}
            record_completed(finished_now)

            queue = _metadata_queue_summary(job_type=args.job_type)
            queued = _nonnegative_batch_count(
                queue.get("queued", 0), field="queue.queued"
            )
            running = _nonnegative_batch_count(
                queue.get("running", 0), field="queue.running"
            )
            inflight_reserved = sum(batch_limit for _, batch_limit in inflight.values())
            available_queued = max(0, queued - inflight_reserved)
            if not inflight:
                if coordinator.limit_reached():
                    break
                if queued <= 0 and running > 0:
                    time.sleep(poll_seconds)
                    continue
                if queued <= 0:
                    break

            while (
                available_queued > 0
                and len(inflight) < max_workers
                and not coordinator.should_stop()
            ):
                batch_limit = coordinator.reserve_batch(max_batch=available_queued)
                if batch_limit <= 0:
                    break
                worker_sequence += 1
                future = executor.submit(
                    _drain_dynamic_batch,
                    worker_index=worker_sequence,
                    args=args,
                    batch_limit=batch_limit,
                )
                inflight[future] = (worker_sequence, batch_limit)
                available_queued -= batch_limit

            if not inflight:
                if coordinator.limit_reached():
                    break
                time.sleep(poll_seconds)
                continue

            done, _pending = wait(
                set(inflight),
                timeout=poll_seconds,
                return_when=FIRST_COMPLETED,
            )
            record_completed(done)

        while inflight:
            done, _pending = wait(set(inflight), return_when=FIRST_COMPLETED)
            record_completed(done)

    summary = {
        "at": datetime.now(timezone.utc).isoformat(),
        "done": True,
        "job_type": args.job_type,
        "dry_run": False,
        "dynamic": True,
        "max_workers": max_workers,
        "processed": coordinator.total_processed,
        "failed": coordinator.total_failed,
        "failed_retryable_jobs": coordinator.total_failed_retryable,
        "failed_final_jobs": coordinator.total_failed_final,
        "html_found": coordinator.total_html_found,
        "batch_errors": coordinator.total_batch_errors,
        "pdf_found": coordinator.total_pdf_found,
        "researchgate_attached": coordinator.total_researchgate_attached,
        "researchgate_downloaded": coordinator.total_researchgate_downloaded,
        "scihub_attached": coordinator.total_scihub_attached,
        "scihub_downloaded": coordinator.total_scihub_downloaded,
        "already_has_pdf": coordinator.total_already_has_pdf,
        "translation_queued": coordinator.total_translation_queued,
        "worker_summaries": list(worker_summaries.values()),
        "log_path": str(log_path),
    }
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False) + "\n")
    _print_json(summary)
    return _job_exit_code(
        failed=coordinator.total_failed,
        batch_errors=coordinator.total_batch_errors,
    )


def _run_single_job_type(args: argparse.Namespace) -> int:
    if args.dynamic_workers and args.workers > 1 and not args.dry_run:
        return _run_dynamic_parallel(args)
    if args.workers > 1 and not args.dry_run:
        return _run_parallel(args)

    processor = _single_worker_processor()
    log_path = Path(args.log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    total_processed = 0
    total_failed = 0
    total_failed_retryable = 0
    total_failed_final = 0
    total_html_found = 0
    total_batch_errors = 0
    total_pdf_found = 0
    total_translation_queued = 0
    total_researchgate_attached = 0
    total_researchgate_downloaded = 0
    total_scihub_attached = 0
    total_scihub_downloaded = 0
    total_already_has_pdf = 0
    limit = None if args.limit <= 0 else args.limit

    while True:
        if limit is not None and total_processed >= limit:
            break
        batch_limit = args.batch_size
        if limit is not None:
            batch_limit = min(batch_limit, limit - total_processed)
        result = _drain_metadata_batch(
            processor,
            job_type=args.job_type,
            limit=batch_limit,
            dry_run=args.dry_run,
        )
        compact = compact_batch(result, job_type=args.job_type)

        total_processed += int(compact.get("processed") or 0)
        total_failed += int(compact.get("failed") or 0)
        total_failed_retryable += int(compact.get("failed_retryable_jobs") or 0)
        total_failed_final += int(compact.get("failed_final_jobs") or 0)
        total_html_found += int(compact.get("html_found") or 0)
        total_batch_errors += int(compact.get("batch_error") is True)
        total_pdf_found += int(compact.get("pdf_found") or 0)
        total_translation_queued += int(compact.get("translation_queued") or 0)
        total_researchgate_attached += int(compact.get("researchgate_attached") or 0)
        total_researchgate_downloaded += int(
            compact.get("researchgate_downloaded") or 0
        )
        total_scihub_attached += int(compact.get("scihub_attached") or 0)
        total_scihub_downloaded += int(compact.get("scihub_downloaded") or 0)
        total_already_has_pdf += int(compact.get("already_has_pdf") or 0)

        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(compact, ensure_ascii=False) + "\n")
        _print_json(compact)

        if int(compact.get("processed") or 0) == 0:
            break
        if args.dry_run:
            break
        if args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)

    summary = {
        "at": datetime.now(timezone.utc).isoformat(),
        "done": True,
        "job_type": args.job_type,
        "dry_run": bool(args.dry_run),
        "processed": total_processed,
        "failed": total_failed,
        "failed_retryable_jobs": total_failed_retryable,
        "failed_final_jobs": total_failed_final,
        "html_found": total_html_found,
        "batch_errors": total_batch_errors,
        "pdf_found": total_pdf_found,
        "researchgate_attached": total_researchgate_attached,
        "researchgate_downloaded": total_researchgate_downloaded,
        "scihub_attached": total_scihub_attached,
        "scihub_downloaded": total_scihub_downloaded,
        "already_has_pdf": total_already_has_pdf,
        "translation_queued": total_translation_queued,
        "log_path": str(log_path),
    }
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False) + "\n")
    _print_json(summary)
    return _job_exit_code(
        failed=total_failed,
        batch_errors=total_batch_errors,
    )


def _run_full_text_pdf_cycle(args: argparse.Namespace) -> int:
    exit_code = 0
    stage_results: list[dict[str, Any]] = []
    for job_type in PDF_FALLBACK_CYCLE:
        if job_type == SCIHUB_PDF_BACKLOG_SCAN:
            if args.dry_run:
                result: dict[str, Any] = {}
                compact = {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "job_type": SCIHUB_PDF_BACKLOG_SCAN,
                    "ok": True,
                    "dry_run": True,
                    "skipped": True,
                    "reason": "dry_run_intake_disabled",
                }
            else:
                processor = ZoteroMetadataProcessor(from_env())
                result = processor.scihub_pdf_backlog_scan(
                    limit=None if args.limit <= 0 else args.limit,
                    force=False,
                )
                compact = {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "job_type": SCIHUB_PDF_BACKLOG_SCAN,
                    "ok": result.get("ok"),
                }
                compact.update(
                    {
                        "scanned": result.get("scanned"),
                        "queued": result.get("queued"),
                        "skipped": result.get("skipped"),
                        "queue": result.get("queue"),
                    }
                )
            log_path = Path(args.log_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(compact, ensure_ascii=False) + "\n")
            _print_json(compact)
            stage_results.append(
                {
                    "job_type": SCIHUB_PDF_BACKLOG_SCAN,
                    "exit_code": 0,
                    "queued": result.get("queued"),
                    "scanned": result.get("scanned"),
                }
            )
            continue
        stage_args = argparse.Namespace(**vars(args))
        stage_args.job_type = job_type
        stage_code = _run_single_job_type(stage_args)
        stage_results.append({"job_type": job_type, "exit_code": stage_code})
        exit_code = max(exit_code, stage_code)

    summary = {
        "at": datetime.now(timezone.utc).isoformat(),
        "done": True,
        "job_type": FULL_TEXT_PDF_CYCLE,
        "stages": stage_results,
        "exit_code": exit_code,
        "log_path": str(Path(args.log_path)),
    }
    log_path = Path(args.log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False) + "\n")
    _print_json(summary)
    return exit_code


def run(args: argparse.Namespace) -> int:
    if args.job_type == FULL_TEXT_PDF_CYCLE:
        return _run_full_text_pdf_cycle(args)
    return _run_single_job_type(args)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Drain metadata jobs with compact JSONL progress output."
    )
    parser.add_argument(
        "--job-type",
        choices=SUPPORTED_RUN_TYPES,
        default=METADATA_JOB_FULL_TEXT,
        help=(
            "Metadata job queue to drain, or full_text_pdf_cycle to run "
            "full_text -> researchgate_pdf -> scihub_pdf."
        ),
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Jobs to process; 0 drains until empty."
    )
    parser.add_argument(
        "--batch-size", type=int, default=5, help="Jobs per processor call."
    )
    parser.add_argument(
        "--sleep-seconds", type=float, default=0.0, help="Pause between batches."
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Parallel metadata workers inside this process. 1 preserves sequential behavior.",
    )
    parser.add_argument(
        "--worker-stagger-seconds",
        type=float,
        default=0.5,
        help="Startup delay between parallel workers to avoid a thundering herd.",
    )
    parser.add_argument(
        "--max-failures",
        type=int,
        default=0,
        help="Stop the parallel pool after this many failed jobs; 0 keeps current behavior.",
    )
    parser.add_argument(
        "--max-retryable-failures",
        type=int,
        default=0,
        help="Stop the parallel pool after this many retryable failed jobs; 0 disables this guard.",
    )
    parser.add_argument(
        "--dynamic-workers",
        action="store_true",
        help="Use --workers as a dynamic maximum and submit only as many batches as the queue needs.",
    )
    parser.add_argument(
        "--dynamic-poll-seconds",
        type=float,
        default=1.0,
        help="How often the dynamic supervisor checks queue depth and completed batches.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--log-path",
        default="/data/ingest/full_text_drain_progress.jsonl",
        help="JSONL progress log path inside the running environment.",
    )
    args = parser.parse_args(argv)
    nonnegative_integers = (
        ("limit", args.limit),
        ("max-failures", args.max_failures),
        ("max-retryable-failures", args.max_retryable_failures),
    )
    for field, value in nonnegative_integers:
        if value < 0:
            parser.error(f"--{field} must be non-negative")
    positive_integers = (
        ("batch-size", args.batch_size),
        ("workers", args.workers),
    )
    for field, value in positive_integers:
        if value <= 0:
            parser.error(f"--{field} must be positive")
    nonnegative_floats = (
        ("sleep-seconds", args.sleep_seconds),
        ("worker-stagger-seconds", args.worker_stagger_seconds),
    )
    for field, value in nonnegative_floats:
        if not math.isfinite(value) or value < 0:
            parser.error(f"--{field} must be finite and non-negative")
    if not math.isfinite(args.dynamic_poll_seconds) or args.dynamic_poll_seconds <= 0:
        parser.error("--dynamic-poll-seconds must be finite and positive")
    if not isinstance(args.log_path, str) or not args.log_path.strip():
        parser.error("--log-path must be a non-empty path")
    return args


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
