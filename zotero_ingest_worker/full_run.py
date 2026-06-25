from __future__ import annotations

import threading
import time
from dataclasses import asdict
from typing import Any

from .config import WorkerConfig
from .full_run_options import FullRunOptions
from .full_run_plan import next_ingest_action
from .full_run_results import _result_failure_count, _result_message, _result_summary
from .metadata_jobs import (
    METADATA_JOB_ARXIV_HTML,
    METADATA_JOB_ENRICH,
    METADATA_JOB_FULL_TEXT,
    METADATA_JOB_RESEARCHGATE_PDF,
    METADATA_JOB_SCIHUB_PDF,
)
from .metadata_processor import ZoteroMetadataProcessor
from .state import PipelineStateStore


class FullRunManager:
    def __init__(self, config: WorkerConfig):
        self.config = config
        self.state = PipelineStateStore(config.state_db_path)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._active_run_id: str | None = None

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        options = FullRunOptions.from_payload(payload)
        with self._lock:
            if self._thread is not None and self._thread.is_alive() and self._active_run_id:
                return {
                    "ok": True,
                    "already_running": True,
                    "run": self.status(self._active_run_id)["run"],
                }

            stale = self.state.full_runs.running()
            if stale is not None:
                self.state.full_runs.update(
                    run_id=str(stale["run_id"]),
                    status="interrupted",
                    phase="interrupted",
                    current_job_kind=None,
                    current_job_id=None,
                    finished=True,
                    event="interrupted",
                    message="Previous ingest run was marked interrupted before a new run started.",
                )

            run = self.state.full_runs.create(options={**asdict(options), "mode": "ingest"})
            run_id = str(run["run_id"])
            self._stop_event = threading.Event()
            self._active_run_id = run_id
            self._thread = threading.Thread(
                target=self._run,
                args=(run_id, options),
                name=f"zotero-ingest-run-{run_id}",
                daemon=True,
            )
            self._thread.start()
        return {"ok": True, "started": True, "run_id": run_id, "run": self.status(run_id)}

    def stop(self, run_id: str | None = None) -> dict[str, Any]:
        target = run_id or self._active_run_id
        if not target:
            latest = self.state.full_runs.latest()
            target = str(latest["run_id"]) if latest else None
        if not target:
            return {"ok": True, "stopped": False, "message": "No ingest run was found."}
        self._stop_event.set()
        run = self.state.full_runs.request_stop(target)
        return {"ok": True, "stop_requested": True, "run": run}

    def status(self, run_id: str | None = None, *, event_limit: int = 50) -> dict[str, Any]:
        run = self.state.full_runs.get(run_id) if run_id else self.state.full_runs.latest()
        metadata_queue = self.state.metadata_jobs.summary(job_type=METADATA_JOB_ENRICH)
        arxiv_html_queue = self.state.metadata_jobs.summary(job_type=METADATA_JOB_ARXIV_HTML)
        full_text_queue = self.state.metadata_jobs.summary(job_type=METADATA_JOB_FULL_TEXT)
        researchgate_pdf_queue = self.state.metadata_jobs.summary(job_type=METADATA_JOB_RESEARCHGATE_PDF)
        scihub_pdf_queue = self.state.metadata_jobs.summary(job_type=METADATA_JOB_SCIHUB_PDF)
        result: dict[str, Any] = {
            "ok": True,
            "run": run,
            "thread_alive": bool(self._thread is not None and self._thread.is_alive()),
            "metadata_queue": metadata_queue,
            "arxiv_html_queue": arxiv_html_queue,
            "full_text_queue": full_text_queue,
            "researchgate_pdf_queue": researchgate_pdf_queue,
            "scihub_pdf_queue": scihub_pdf_queue,
            "running_metadata_jobs": self.state.metadata_jobs.list(
                job_type=METADATA_JOB_ENRICH,
                statuses={"running"},
                limit=5,
            ),
            "running_arxiv_html_jobs": self.state.metadata_jobs.list(
                job_type=METADATA_JOB_ARXIV_HTML,
                statuses={"running"},
                limit=5,
            ),
            "running_full_text_jobs": self.state.metadata_jobs.list(
                job_type=METADATA_JOB_FULL_TEXT,
                statuses={"running"},
                limit=5,
            ),
            "running_researchgate_pdf_jobs": self.state.metadata_jobs.list(
                job_type=METADATA_JOB_RESEARCHGATE_PDF,
                statuses={"running"},
                limit=5,
            ),
            "running_scihub_pdf_jobs": self.state.metadata_jobs.list(
                job_type=METADATA_JOB_SCIHUB_PDF,
                statuses={"running"},
                limit=5,
            ),
        }
        if run:
            result["events"] = self.state.full_runs.events(
                str(run["run_id"]),
                limit=event_limit,
            )
        return result

    def _run(self, run_id: str, options: FullRunOptions) -> None:
        metadata = ZoteroMetadataProcessor(self.config)
        last_intake = 0.0
        idle_cycles = 0
        run_processed = 0
        run_failed = 0
        scihub_pdf_backlog_scanned = False

        try:
            self.state.update_full_run(
                run_id=run_id,
                phase="running",
                current_job_kind=None,
                current_job_id=None,
                event="running",
                message="Ingest controller entered the processing loop.",
            )
            while not self._stop_event.is_set() and not self.state.full_run_stop_requested(run_id):
                now = time.monotonic()
                if now - last_intake >= options.intake_interval_seconds:
                    self._run_intake(run_id, options)
                    last_intake = now
                    scihub_pdf_backlog_scanned = False

                metadata_queue = self.state.metadata_queue_summary(job_type=METADATA_JOB_ENRICH)
                arxiv_html_queue = self.state.metadata_queue_summary(job_type=METADATA_JOB_ARXIV_HTML)
                full_text_queue = self.state.metadata_queue_summary(job_type=METADATA_JOB_FULL_TEXT)
                researchgate_pdf_queue = self.state.metadata_queue_summary(
                    job_type=METADATA_JOB_RESEARCHGATE_PDF
                )
                scihub_pdf_queue = self.state.metadata_queue_summary(
                    job_type=METADATA_JOB_SCIHUB_PDF
                )
                running_jobs = (
                    int(metadata_queue.get("running") or 0)
                    + int(arxiv_html_queue.get("running") or 0)
                    + int(full_text_queue.get("running") or 0)
                    + int(researchgate_pdf_queue.get("running") or 0)
                    + int(scihub_pdf_queue.get("running") or 0)
                )
                if running_jobs > 0:
                    self.state.update_full_run(
                        run_id=run_id,
                        phase="waiting_for_running_job",
                        current_job_kind=None,
                        current_job_id=None,
                    )
                    time.sleep(options.poll_seconds)
                    continue

                action = self._next_action(
                    options,
                    metadata_queue=metadata_queue,
                    arxiv_html_queue=arxiv_html_queue,
                    full_text_queue=full_text_queue,
                    researchgate_pdf_queue=researchgate_pdf_queue,
                    scihub_pdf_queue=scihub_pdf_queue,
                    scihub_pdf_backlog_pending=(
                        options.scihub_pdf_backlog_intake
                        and not scihub_pdf_backlog_scanned
                    ),
                )
                if action is not None:
                    result = self._drain_action(
                        run_id=run_id,
                        action=action,
                        options=options,
                        metadata=metadata,
                    )
                    if action == "scihub_pdf_backlog":
                        scihub_pdf_backlog_scanned = True
                    else:
                        run_processed += int(result.get("processed") or 0)
                        run_failed += _result_failure_count(result)
                    idle_cycles = 0
                    continue

                idle_cycles += 1
                idle_metadata = {
                    "metadata_queue": metadata_queue,
                    "arxiv_html_queue": arxiv_html_queue,
                    "full_text_queue": full_text_queue,
                    "researchgate_pdf_queue": researchgate_pdf_queue,
                    "scihub_pdf_queue": scihub_pdf_queue,
                }
                self.state.update_full_run(
                    run_id=run_id,
                    phase="idle",
                    current_job_kind=None,
                    current_job_id=None,
                    event="idle",
                    message=f"No queued ingest work. idle_cycles={idle_cycles}.",
                    metadata=idle_metadata,
                )
                if options.stop_when_idle and idle_cycles >= options.idle_cycles_to_complete:
                    completed_with_errors = run_failed > 0
                    self.state.update_full_run(
                        run_id=run_id,
                        status="completed_with_errors" if completed_with_errors else "succeeded",
                        phase="complete",
                        current_job_kind=None,
                        current_job_id=None,
                        finished=True,
                        event="complete_with_errors" if completed_with_errors else "complete",
                        message=(
                            "Ingest run completed with errors because all queues were idle "
                            f"(processed={run_processed}, failed={run_failed})."
                            if completed_with_errors
                            else "Ingest run completed because all queues were idle."
                        ),
                        metadata={
                            "processed": run_processed,
                            "failed": run_failed,
                            **idle_metadata,
                        },
                    )
                    return
                time.sleep(options.poll_seconds)

            self.state.update_full_run(
                run_id=run_id,
                status="stopped",
                phase="stopped",
                current_job_kind=None,
                current_job_id=None,
                finished=True,
                event="stopped",
                message="Ingest run stopped by request.",
            )
        except Exception as exc:
            self.state.update_full_run(
                run_id=run_id,
                status="failed",
                phase="failed",
                current_job_kind=None,
                current_job_id=None,
                last_error=str(exc),
                finished=True,
                event="failed",
                message=str(exc),
            )
        finally:
            with self._lock:
                if self._active_run_id == run_id:
                    self._active_run_id = None

    def _run_intake(self, run_id: str, options: FullRunOptions) -> None:
        self._record_stage(
            run_id=run_id,
            phase="intake",
            message="Ingest queue intake tick started.",
        )
        metadata = ZoteroMetadataProcessor(self.config)
        results: dict[str, Any] = {}
        if options.metadata_backlog_intake:
            results["metadata_backlog_scan"] = metadata.metadata_backlog_scan(
                max_items=options.max_items,
                limit=options.queue_limit,
                force=options.force,
            )
        if options.full_text_backlog_intake:
            results["full_text_backlog_scan"] = metadata.full_text_backlog_scan(
                max_items=options.max_items,
                limit=options.queue_limit,
                force=options.force,
            )
        if options.arxiv_html_backlog_intake:
            results["arxiv_html_backlog_scan"] = metadata.arxiv_html_backlog_scan(
                max_items=options.max_items,
                limit=options.queue_limit,
                force=options.force,
            )
        self._record_stage(
            run_id=run_id,
            phase="intake_done",
            message="Ingest queue intake tick finished.",
            metadata={key: _result_summary(value) for key, value in results.items()},
        )

    def _record_stage(
        self,
        run_id: str,
        *,
        phase: str,
        message: str,
        event: str | None = None,
        current_job_kind: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.state.update_full_run(
            run_id=run_id,
            phase=phase,
            current_job_kind=current_job_kind,
            current_job_id=None,
            event=event or phase,
            message=message,
            metadata=metadata,
        )

    def _drain_action(
        self,
        *,
        run_id: str,
        action: str,
        options: FullRunOptions,
        metadata: ZoteroMetadataProcessor,
    ) -> dict[str, Any]:
        actions = {
            "metadata": (
                "draining_metadata",
                "metadata",
                "Draining one metadata enrichment batch.",
                lambda: metadata.drain_metadata_queue(
                    limit=options.drain_limit,
                    dry_run=options.dry_run,
                    require_relay=options.require_relay,
                ),
            ),
            "arxiv_html": (
                "draining_arxiv_html",
                "arxiv_html",
                "Draining one arXiv HTML batch.",
                lambda: metadata.drain_arxiv_html_queue(
                    limit=options.drain_limit,
                    dry_run=options.dry_run,
                    require_relay=options.require_relay,
                ),
            ),
            "full_text": (
                "draining_full_text",
                "full_text",
                "Draining one full-text discovery batch.",
                lambda: metadata.drain_full_text_queue(
                    limit=options.drain_limit,
                    dry_run=options.dry_run,
                ),
            ),
            "researchgate_pdf": (
                "draining_researchgate_pdf",
                "researchgate_pdf",
                "Draining one ResearchGate PDF browser batch.",
                lambda: metadata.drain_researchgate_pdf_queue(
                    limit=options.drain_limit,
                    dry_run=options.dry_run,
                    require_relay=options.require_relay,
                ),
            ),
            "scihub_pdf_backlog": (
                "scanning_scihub_pdf_backlog",
                "scihub_pdf_backlog",
                "Scanning remaining parent items without PDF for Sci-Hub fallback.",
                lambda: metadata.scihub_pdf_backlog_scan(
                    max_items=options.max_items,
                    limit=options.queue_limit,
                    force=options.force,
                ),
            ),
            "scihub_pdf": (
                "draining_scihub_pdf",
                "scihub_pdf",
                "Draining one Sci-Hub PDF fallback batch.",
                lambda: metadata.drain_scihub_pdf_queue(
                    limit=options.drain_limit,
                    dry_run=options.dry_run,
                    require_relay=options.require_relay,
                ),
            ),
        }
        if action not in actions:
            raise ValueError(f"Unsupported ingest action: {action}")

        phase, kind, message, callback = actions[action]
        self._record_stage(
            run_id=run_id,
            phase=phase,
            current_job_kind=kind,
            message=message,
        )
        result = callback()
        self._record_stage(
            run_id=run_id,
            phase=f"{phase}_done",
            message=_result_message(result),
            metadata=_result_summary(result),
        )
        return result

    @staticmethod
    def _next_action(
        options: FullRunOptions,
        *,
        metadata_queue: dict[str, Any] | None = None,
        arxiv_html_queue: dict[str, Any] | None = None,
        full_text_queue: dict[str, Any] | None = None,
        researchgate_pdf_queue: dict[str, Any] | None = None,
        scihub_pdf_queue: dict[str, Any] | None = None,
        scihub_pdf_backlog_pending: bool = False,
    ) -> str | None:
        return next_ingest_action(
            options,
            metadata_queue=metadata_queue,
            arxiv_html_queue=arxiv_html_queue,
            full_text_queue=full_text_queue,
            researchgate_pdf_queue=researchgate_pdf_queue,
            scihub_pdf_queue=scihub_pdf_queue,
            scihub_pdf_backlog_pending=scihub_pdf_backlog_pending,
        )
