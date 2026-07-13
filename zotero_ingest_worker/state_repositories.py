from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WatchFileRepository:
    store: Any

    def count(self) -> int:
        return self.store.watch_count()

    def get(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        return self.store.get_watched_file(*args, **kwargs)

    def list(self) -> dict[str, dict[str, Any]]:
        return self.store.list_watched_files()

    def is_unchanged(self, *args: Any, **kwargs: Any) -> bool:
        return self.store.is_watched_file_unchanged(*args, **kwargs)

    def mark(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.mark_watched_file(*args, **kwargs)


@dataclass(frozen=True)
class ProblemDocumentRepository:
    store: Any

    def record(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.record_problem_document(*args, **kwargs)

    def resolve(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        return self.store.resolve_problem_document(*args, **kwargs)

    def list(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self.store.list_problem_documents(*args, **kwargs)


@dataclass(frozen=True)
class OcrJobRepository:
    store: Any

    def enqueue(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.enqueue_job(*args, **kwargs)

    def get(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        return self.store.get_job(*args, **kwargs)

    def get_by_dedupe_key(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        return self.store.get_job_by_dedupe_key(*args, **kwargs)

    def list(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self.store.list_jobs(*args, **kwargs)

    def summary(self) -> dict[str, Any]:
        return self.store.queue_summary()

    def lease_next(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        return self.store.lease_next_job(*args, **kwargs)

    def mark_succeeded(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.mark_job_succeeded(*args, **kwargs)

    def mark_failed(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.mark_job_failed(*args, **kwargs)

    def mark_manual_review(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.mark_job_manual_review(*args, **kwargs)

    def mark_problem_document(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.mark_job_problem_document(*args, **kwargs)

    def mark_progress(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.mark_job_progress(*args, **kwargs)

    def retry(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.retry_job(*args, **kwargs)

    def cancel(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.cancel_job(*args, **kwargs)


@dataclass(frozen=True)
class HtmlJobRepository:
    store: Any

    def enqueue(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.enqueue_html_job(*args, **kwargs)

    def get(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        return self.store.get_html_job(*args, **kwargs)

    def get_by_dedupe_key(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        return self.store.get_html_job_by_dedupe_key(*args, **kwargs)

    def list(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self.store.list_html_jobs(*args, **kwargs)

    def summary(self) -> dict[str, Any]:
        return self.store.html_queue_summary()

    def lease_next(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        return self.store.lease_next_html_job(*args, **kwargs)

    def mark_succeeded(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.mark_html_job_succeeded(*args, **kwargs)

    def mark_deferred(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.mark_html_job_deferred(*args, **kwargs)

    def mark_failed(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.mark_html_job_failed(*args, **kwargs)

    def heartbeat(self, *args: Any, **kwargs: Any) -> bool:
        return self.store.heartbeat_html_job(*args, **kwargs)

    def retry(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.retry_html_job(*args, **kwargs)

    def cancel(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.cancel_html_job(*args, **kwargs)


@dataclass(frozen=True)
class MetadataJobRepository:
    store: Any

    def enqueue(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.enqueue_metadata_job(*args, **kwargs)

    def get(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        return self.store.get_metadata_job(*args, **kwargs)

    def get_by_dedupe_key(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        return self.store.get_metadata_job_by_dedupe_key(*args, **kwargs)

    def list(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self.store.list_metadata_jobs(*args, **kwargs)

    def summary(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.metadata_queue_summary(*args, **kwargs)

    def lease_next(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        return self.store.lease_next_metadata_job(*args, **kwargs)

    def mark_succeeded(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.mark_metadata_job_succeeded(*args, **kwargs)

    def mark_skipped(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.mark_metadata_job_skipped(*args, **kwargs)

    def mark_failed(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.mark_metadata_job_failed(*args, **kwargs)

    def retry(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.retry_metadata_job(*args, **kwargs)

    def cancel(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.cancel_metadata_job(*args, **kwargs)


@dataclass(frozen=True)
class FullRunRepository:
    store: Any

    def create(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.create_full_run(*args, **kwargs)

    def get(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        return self.store.get_full_run(*args, **kwargs)

    def latest(self) -> dict[str, Any] | None:
        return self.store.latest_full_run()

    def running(self) -> dict[str, Any] | None:
        return self.store.running_full_run()

    def update(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        return self.store.update_full_run(*args, **kwargs)

    def request_stop(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.request_full_run_stop(*args, **kwargs)

    def stop_requested(self, *args: Any, **kwargs: Any) -> bool:
        return self.store.full_run_stop_requested(*args, **kwargs)

    def events(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self.store.list_full_run_events(*args, **kwargs)
