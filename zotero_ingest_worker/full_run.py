from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from dataclasses import asdict
from typing import Any, Callable

from .config import WorkerConfig
from .full_run_options import FullRunOptions
from .full_run_plan import next_ingest_action, ready_ingest_actions
from .full_run_results import _result_failure_count, _result_message, _result_summary
from .metadata_jobs import (
    METADATA_JOB_ARXIV_HTML,
    METADATA_JOB_ENRICH,
    METADATA_JOB_FULL_TEXT,
    METADATA_JOB_RESEARCHGATE_PDF,
    METADATA_JOB_SCIHUB_PDF,
)
from .metadata_processor import ZoteroMetadataProcessor
from .state import (
    DEFAULT_FULL_RUN_STALE_AFTER_SECONDS,
    MAX_FULL_RUN_EVENT_LIMIT,
    PipelineStateStore,
)

_FULL_RUN_HEARTBEAT_INTERVAL_SECONDS = 30
_LOGGER = logging.getLogger(__name__)


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
            if (
                self._thread is not None
                and self._thread.is_alive()
                and self._active_run_id
            ):
                run_id = self._active_run_id
                run = self.state.full_runs.get(run_id)
                if run is None:
                    raise RuntimeError(
                        f"active full-run thread has no state row: {run_id}"
                    )
                return {
                    "ok": True,
                    "started": False,
                    "already_running": True,
                    "run_id": run_id,
                    "run": run,
                }

            claimed = self.state.full_runs.create(
                options={**asdict(options), "mode": "ingest"},
                stale_after_seconds=DEFAULT_FULL_RUN_STALE_AFTER_SECONDS,
            )
            created = claimed.get("created")
            if type(created) is not bool:
                raise RuntimeError("full-run claim returned an invalid created flag")
            run_id = str(claimed["run_id"])
            run = {key: value for key, value in claimed.items() if key != "created"}
            if not created:
                return {
                    "ok": True,
                    "started": False,
                    "already_running": True,
                    "run_id": run_id,
                    "run": run,
                }

            self._stop_event = threading.Event()
            self._active_run_id = run_id
            self._thread = threading.Thread(
                target=self._run,
                args=(run_id, options),
                name=f"zotero-ingest-run-{run_id}",
                daemon=True,
            )
            try:
                self._thread.start()
            except BaseException as exc:
                self._thread = None
                self._active_run_id = None
                self.state.full_runs.update(
                    run_id=run_id,
                    status="failed",
                    phase="thread_start_failed",
                    last_error=str(exc) or type(exc).__name__,
                    finished=True,
                    event="thread_start_failed",
                    message="Full-run controller thread failed to start.",
                )
                raise
        return {
            "ok": True,
            "started": True,
            "already_running": False,
            "run_id": run_id,
            "run": run,
        }

    def stop(self, run_id: str | None = None) -> dict[str, Any]:
        target = run_id or self._active_run_id
        if not target:
            latest = self.state.full_runs.latest()
            target = str(latest["run_id"]) if latest else None
        if not target:
            return {"ok": True, "stopped": False, "message": "No ingest run was found."}
        run = self.state.full_runs.request_stop(target)
        stop_requested = bool(
            run
            and str(run.get("status") or "") in {"running", "stopping"}
            and int(run.get("stop_requested") or 0) == 1
            and not run.get("finished_at")
        )
        if stop_requested and target == self._active_run_id:
            self._stop_event.set()
        result: dict[str, Any] = {
            "ok": True,
            "stop_requested": stop_requested,
            "run": run,
        }
        if not stop_requested:
            result["message"] = "The selected ingest run is not active."
        return result

    def status(
        self, run_id: str | None = None, *, event_limit: int = 50
    ) -> dict[str, Any]:
        safe_event_limit = _validated_full_run_event_limit(event_limit)
        run = (
            self.state.full_runs.get(run_id)
            if run_id
            else self.state.full_runs.latest()
        )
        metadata_queue = self.state.metadata_jobs.summary(job_type=METADATA_JOB_ENRICH)
        arxiv_html_queue = self.state.metadata_jobs.summary(
            job_type=METADATA_JOB_ARXIV_HTML
        )
        full_text_queue = self.state.metadata_jobs.summary(
            job_type=METADATA_JOB_FULL_TEXT
        )
        researchgate_pdf_queue = self.state.metadata_jobs.summary(
            job_type=METADATA_JOB_RESEARCHGATE_PDF
        )
        scihub_pdf_queue = self.state.metadata_jobs.summary(
            job_type=METADATA_JOB_SCIHUB_PDF
        )
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
                limit=safe_event_limit,
            )
        return result

    def _run(self, run_id: str, options: FullRunOptions) -> None:
        last_intake = 0.0
        idle_cycles = 0
        run_processed = 0
        run_failed = 0
        scihub_pdf_backlog_scanned = False
        run_stop_event = self._stop_event
        heartbeat_stop = threading.Event()
        heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(run_id, heartbeat_stop, run_stop_event),
            name=f"zotero-ingest-run-heartbeat-{run_id}",
            daemon=True,
        )
        heartbeat_started = False

        try:
            heartbeat_thread.start()
            heartbeat_started = True
            if options.dry_run:
                self._run_dry_run(run_id, options)
                return
            self._record_stage(
                run_id=run_id,
                phase="running",
                message="Ingest controller entered the processing loop.",
            )
            while (
                not run_stop_event.is_set()
                and not self.state.full_run_stop_requested(run_id)
            ):
                now = time.monotonic()
                if now - last_intake >= options.intake_interval_seconds:
                    self._run_intake(run_id, options)
                    last_intake = now
                    scihub_pdf_backlog_scanned = False

                metadata_queue = self.state.metadata_queue_summary(
                    job_type=METADATA_JOB_ENRICH
                )
                arxiv_html_queue = self.state.metadata_queue_summary(
                    job_type=METADATA_JOB_ARXIV_HTML
                )
                full_text_queue = self.state.metadata_queue_summary(
                    job_type=METADATA_JOB_FULL_TEXT
                )
                researchgate_pdf_queue = self.state.metadata_queue_summary(
                    job_type=METADATA_JOB_RESEARCHGATE_PDF
                )
                scihub_pdf_queue = self.state.metadata_queue_summary(
                    job_type=METADATA_JOB_SCIHUB_PDF
                )
                actions = self._ready_actions(
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
                if actions:
                    results = self._drain_parallel_actions(
                        run_id=run_id,
                        actions=actions,
                        options=options,
                    )
                    if "scihub_pdf_backlog" in results:
                        scihub_pdf_backlog_scanned = True
                    for action, result in results.items():
                        if action == "scihub_pdf_backlog":
                            continue
                        run_processed += int(result.get("processed") or 0)
                        run_failed += _result_failure_count(result)
                    idle_cycles = 0
                    continue

                running_jobs = self._running_jobs(
                    metadata_queue=metadata_queue,
                    arxiv_html_queue=arxiv_html_queue,
                    full_text_queue=full_text_queue,
                    researchgate_pdf_queue=researchgate_pdf_queue,
                    scihub_pdf_queue=scihub_pdf_queue,
                )
                if running_jobs > 0:
                    self._record_stage(
                        run_id=run_id,
                        phase="waiting_for_running_job",
                        message="Ingest run is waiting for active metadata jobs to finish.",
                    )
                    time.sleep(options.poll_seconds)
                    continue

                idle_cycles += 1
                idle_metadata = {
                    "metadata_queue": metadata_queue,
                    "arxiv_html_queue": arxiv_html_queue,
                    "full_text_queue": full_text_queue,
                    "researchgate_pdf_queue": researchgate_pdf_queue,
                    "scihub_pdf_queue": scihub_pdf_queue,
                }
                self._record_stage(
                    run_id=run_id,
                    phase="idle",
                    message=f"No queued ingest work. idle_cycles={idle_cycles}.",
                    metadata=idle_metadata,
                )
                if (
                    options.stop_when_idle
                    and idle_cycles >= options.idle_cycles_to_complete
                ):
                    completed_with_errors = run_failed > 0
                    self._record_stage(
                        run_id=run_id,
                        status="completed_with_errors"
                        if completed_with_errors
                        else "succeeded",
                        phase="complete",
                        finished=True,
                        event="complete_with_errors"
                        if completed_with_errors
                        else "complete",
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

            self._record_stage(
                run_id=run_id,
                status="stopped",
                phase="stopped",
                finished=True,
                message="Ingest run stopped by request.",
            )
        except BaseException as exc:
            self._record_stage(
                run_id=run_id,
                status="failed",
                phase="failed",
                last_error=str(exc),
                finished=True,
                message=str(exc),
            )
            if not isinstance(exc, Exception):
                raise
        finally:
            heartbeat_stop.set()
            if heartbeat_started:
                heartbeat_thread.join(timeout=_FULL_RUN_HEARTBEAT_INTERVAL_SECONDS + 5)
            with self._lock:
                if self._active_run_id == run_id:
                    self._active_run_id = None

    def _heartbeat_loop(
        self,
        run_id: str,
        stop_event: threading.Event,
        run_stop_event: threading.Event,
    ) -> None:
        while not stop_event.is_set():
            try:
                active = self.state.full_runs.heartbeat(run_id)
            except Exception:
                _LOGGER.exception(
                    "Full-run heartbeat failed; stopping owned run %s before its "
                    "ownership can expire.",
                    run_id,
                )
                run_stop_event.set()
                return
            if not active:
                run_stop_event.set()
                return
            if stop_event.wait(_FULL_RUN_HEARTBEAT_INTERVAL_SECONDS):
                return

    def _run_dry_run(self, run_id: str, options: FullRunOptions) -> None:
        self._record_stage(
            run_id=run_id,
            phase="dry_run",
            message="Read-only ingest queue preview started; backlog intake is disabled.",
        )
        metadata_queue = self.state.metadata_queue_summary(job_type=METADATA_JOB_ENRICH)
        arxiv_html_queue = self.state.metadata_queue_summary(
            job_type=METADATA_JOB_ARXIV_HTML
        )
        full_text_queue = self.state.metadata_queue_summary(
            job_type=METADATA_JOB_FULL_TEXT
        )
        researchgate_pdf_queue = self.state.metadata_queue_summary(
            job_type=METADATA_JOB_RESEARCHGATE_PDF
        )
        scihub_pdf_queue = self.state.metadata_queue_summary(
            job_type=METADATA_JOB_SCIHUB_PDF
        )
        actions = self._ready_actions(
            options,
            metadata_queue=metadata_queue,
            arxiv_html_queue=arxiv_html_queue,
            full_text_queue=full_text_queue,
            researchgate_pdf_queue=researchgate_pdf_queue,
            scihub_pdf_queue=scihub_pdf_queue,
            scihub_pdf_backlog_pending=False,
        )
        results = self._drain_parallel_actions(
            run_id=run_id,
            actions=actions,
            options=options,
        )
        self._record_stage(
            run_id=run_id,
            status="succeeded",
            phase="dry_run_complete",
            finished=True,
            event="dry_run_complete",
            message=(
                "Read-only ingest queue preview completed; no backlog jobs were enqueued "
                "and no queued jobs were leased."
            ),
            metadata={
                "dry_run": True,
                "backlog_intake_disabled": True,
                "actions": {
                    action: _result_summary(result)
                    for action, result in results.items()
                },
                "metadata_queue": metadata_queue,
                "arxiv_html_queue": arxiv_html_queue,
                "full_text_queue": full_text_queue,
                "researchgate_pdf_queue": researchgate_pdf_queue,
                "scihub_pdf_queue": scihub_pdf_queue,
            },
        )

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
        status: str | None = None,
        event: str | None = None,
        current_job_kind: str | None = None,
        current_job_id: str | None = None,
        last_error: str | None = None,
        finished: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.state.update_full_run(
            run_id=run_id,
            status=status,
            phase=phase,
            current_job_kind=current_job_kind,
            current_job_id=current_job_id,
            last_error=last_error,
            finished=finished,
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
        actions: dict[str, tuple[str, str, str, Callable[[], dict[str, Any]]]] = {
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
                    require_relay=options.require_relay,
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

    def _drain_parallel_actions(
        self,
        *,
        run_id: str,
        actions: list[str],
        options: FullRunOptions,
    ) -> dict[str, dict[str, Any]]:
        if not actions:
            return {}
        if len(actions) == 1:
            action = actions[0]
            return {
                action: self._drain_action(
                    run_id=run_id,
                    action=action,
                    options=options,
                    metadata=ZoteroMetadataProcessor(self.config),
                )
            }

        results: dict[str, dict[str, Any]] = {}
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(actions)
        ) as executor:
            futures = {
                executor.submit(
                    self._drain_action,
                    run_id=run_id,
                    action=action,
                    options=options,
                    metadata=ZoteroMetadataProcessor(self.config),
                ): action
                for action in actions
            }
            for future in concurrent.futures.as_completed(futures):
                action = futures[future]
                results[action] = future.result()
        return results

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

    @staticmethod
    def _ready_actions(
        options: FullRunOptions,
        *,
        metadata_queue: dict[str, Any] | None = None,
        arxiv_html_queue: dict[str, Any] | None = None,
        full_text_queue: dict[str, Any] | None = None,
        researchgate_pdf_queue: dict[str, Any] | None = None,
        scihub_pdf_queue: dict[str, Any] | None = None,
        scihub_pdf_backlog_pending: bool = False,
    ) -> list[str]:
        return ready_ingest_actions(
            options,
            metadata_queue=metadata_queue,
            arxiv_html_queue=arxiv_html_queue,
            full_text_queue=full_text_queue,
            researchgate_pdf_queue=researchgate_pdf_queue,
            scihub_pdf_queue=scihub_pdf_queue,
            scihub_pdf_backlog_pending=scihub_pdf_backlog_pending,
        )

    @staticmethod
    def _running_jobs(
        *,
        metadata_queue: dict[str, Any],
        arxiv_html_queue: dict[str, Any],
        full_text_queue: dict[str, Any],
        researchgate_pdf_queue: dict[str, Any],
        scihub_pdf_queue: dict[str, Any],
    ) -> int:
        return (
            int(metadata_queue.get("running") or 0)
            + int(arxiv_html_queue.get("running") or 0)
            + int(full_text_queue.get("running") or 0)
            + int(researchgate_pdf_queue.get("running") or 0)
            + int(scihub_pdf_queue.get("running") or 0)
        )


def _validated_full_run_event_limit(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_FULL_RUN_EVENT_LIMIT:
        raise ValueError(
            f"event_limit must be an integer between 0 and {MAX_FULL_RUN_EVENT_LIMIT}"
        )
    return value
