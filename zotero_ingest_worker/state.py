from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .state_schema import (
    PIPELINE_STATE_SCHEMA_VERSION as PIPELINE_STATE_SCHEMA_VERSION,
    initialize_pipeline_state_schema,
)

_EVENT_HISTORY_TABLES = {
    "ocr_job_events": "job_id",
    "html_job_events": "job_id",
    "metadata_job_events": "job_id",
    "full_run_events": "run_id",
}
_EVENT_MAINTENANCE_KEY = "event_history"
_METADATA_RESULT_MAINTENANCE_KEY = "metadata_result_history"
_EVENT_RETENTION_DAYS = 14
_EVENT_KEEP_PER_ENTITY = 20
_EVENT_PRUNE_BATCH_SIZE = 5_000
_METADATA_RESULT_MAX_BYTES = 64 * 1024
_METADATA_RESULT_COMPACT_BATCH_SIZE = 100
_MAINTENANCE_INTERVAL_SECONDS = 3_600
_MAINTENANCE_BACKLOG_INTERVAL_SECONDS = 60
_COMPACT_PREVIEW_MAX_DEPTH = 3
_COMPACT_PREVIEW_MAX_ITEMS = 8
_COMPACT_PREVIEW_MAX_STRING_CHARS = 1_024
_MAX_DOWNSTREAM_REFS = 64
_MAX_COMPACT_SCAN_NODES = 200_000

if TYPE_CHECKING:
    from .state_repositories import (
        FullRunRepository,
        HtmlJobRepository,
        MetadataJobRepository,
        OcrJobRepository,
        ProblemDocumentRepository,
        WatchFileRepository,
    )


@dataclass(frozen=True)
class FileSignature:
    size: int
    mtime_ns: int

    @classmethod
    def from_path(cls, path: Path) -> "FileSignature":
        stat = path.stat()
        return cls(size=stat.st_size, mtime_ns=stat.st_mtime_ns)


class PipelineStateStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def schema_version(self) -> int:
        with self._connect() as connection:
            row = connection.execute("pragma user_version").fetchone()
        return int(row[0])

    @property
    def watch_files(self) -> "WatchFileRepository":
        from .state_repositories import WatchFileRepository

        return WatchFileRepository(self)

    @property
    def problem_documents(self) -> "ProblemDocumentRepository":
        from .state_repositories import ProblemDocumentRepository

        return ProblemDocumentRepository(self)

    @property
    def ocr_jobs(self) -> "OcrJobRepository":
        from .state_repositories import OcrJobRepository

        return OcrJobRepository(self)

    @property
    def html_jobs(self) -> "HtmlJobRepository":
        from .state_repositories import HtmlJobRepository

        return HtmlJobRepository(self)

    @property
    def metadata_jobs(self) -> "MetadataJobRepository":
        from .state_repositories import MetadataJobRepository

        return MetadataJobRepository(self)

    @property
    def full_runs(self) -> "FullRunRepository":
        from .state_repositories import FullRunRepository

        return FullRunRepository(self)

    def get(self, key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "select * from ocr_state where attachment_key = ?",
                (key,),
            ).fetchone()
        return dict(row) if row is not None else None

    def is_unchanged(self, key: str, signature: FileSignature) -> bool:
        row = self.get(key)
        if row is None:
            return False
        return bool(
            row["source_size"] == signature.size
            and row["source_mtime_ns"] == signature.mtime_ns
        )

    def mark(
        self,
        *,
        key: str,
        source_path: Path,
        status: str,
        message: str,
        text_chars: int = 0,
        result_path: Path | None = None,
        backup_path: Path | None = None,
    ) -> dict[str, Any]:
        signature = FileSignature.from_path(source_path)
        updated_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                insert into ocr_state (
                  attachment_key,
                  source_path,
                  source_size,
                  source_mtime_ns,
                  status,
                  message,
                  text_chars,
                  result_path,
                  backup_path,
                  updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(attachment_key) do update set
                  source_path = excluded.source_path,
                  source_size = excluded.source_size,
                  source_mtime_ns = excluded.source_mtime_ns,
                  status = excluded.status,
                  message = excluded.message,
                  text_chars = excluded.text_chars,
                  result_path = excluded.result_path,
                  backup_path = excluded.backup_path,
                  updated_at = excluded.updated_at
                """,
                (
                    key,
                    str(source_path),
                    signature.size,
                    signature.mtime_ns,
                    status,
                    message,
                    text_chars,
                    str(result_path) if result_path is not None else None,
                    str(backup_path) if backup_path is not None else None,
                    updated_at,
                ),
            )
        return self.get(key) or {}

    def watch_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("select count(*) from watch_state").fetchone()
        return int(row[0])

    def get_watched_file(self, source_path: Path) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "select * from watch_state where source_path = ?",
                (str(source_path.resolve()),),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_watched_files(self) -> dict[str, dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("select * from watch_state").fetchall()
        return {str(row["source_path"]): dict(row) for row in rows}

    def is_watched_file_unchanged(self, source_path: Path, signature: FileSignature) -> bool:
        row = self.get_watched_file(source_path)
        if row is None:
            return False
        return bool(
            row["source_size"] == signature.size
            and row["source_mtime_ns"] == signature.mtime_ns
        )

    def mark_watched_file(
        self,
        *,
        source_path: Path,
        signature: FileSignature,
        status: str,
        message: str,
        attachment_key: str | None = None,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        updated_at = _utc_now().isoformat()
        resolved_source = str(source_path.resolve())
        previous = self.get_watched_file(source_path)
        if (
            previous
            and int(previous["source_size"]) == signature.size
            and int(previous["source_mtime_ns"]) == signature.mtime_ns
        ):
            stable_seen_count = int(previous.get("stable_seen_count") or 0) + 1
        else:
            stable_seen_count = 1
        first_seen_at = (
            str(previous.get("first_seen_at") or previous.get("updated_at"))
            if previous
            else updated_at
        )
        with self._connect() as connection:
            connection.execute(
                """
                insert into watch_state (
                  source_path,
                  source_size,
                  source_mtime_ns,
                  status,
                  message,
                  attachment_key,
                  stable_seen_count,
                  first_seen_at,
                  last_seen_at,
                  last_error,
                  updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(source_path) do update set
                  source_size = excluded.source_size,
                  source_mtime_ns = excluded.source_mtime_ns,
                  status = excluded.status,
                  message = excluded.message,
                  attachment_key = excluded.attachment_key,
                  stable_seen_count = excluded.stable_seen_count,
                  first_seen_at = coalesce(watch_state.first_seen_at, excluded.first_seen_at),
                  last_seen_at = excluded.last_seen_at,
                  last_error = excluded.last_error,
                  updated_at = excluded.updated_at
                """,
                (
                    resolved_source,
                    signature.size,
                    signature.mtime_ns,
                    status,
                    message,
                    attachment_key,
                    stable_seen_count,
                    first_seen_at,
                    updated_at,
                    last_error,
                    updated_at,
                ),
            )
        return self.get_watched_file(source_path) or {}

    def record_problem_document(
        self,
        *,
        library_id: str,
        attachment_key: str,
        data_dir: Path,
        source_path: Path,
        signature: FileSignature,
        problem_status: str,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _utc_now().isoformat()
        resolved_source = str(source_path.resolve())
        with self._connect() as connection:
            connection.execute(
                """
                insert into problem_documents (
                  source_path,
                  library_id,
                  attachment_key,
                  data_dir,
                  source_size,
                  source_mtime_ns,
                  problem_status,
                  reason,
                  first_seen_at,
                  last_seen_at,
                  resolved_at,
                  metadata
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, null, ?)
                on conflict(source_path) do update set
                  library_id = excluded.library_id,
                  attachment_key = excluded.attachment_key,
                  data_dir = excluded.data_dir,
                  source_size = excluded.source_size,
                  source_mtime_ns = excluded.source_mtime_ns,
                  problem_status = excluded.problem_status,
                  reason = excluded.reason,
                  last_seen_at = excluded.last_seen_at,
                  resolved_at = null,
                  metadata = excluded.metadata
                """,
                (
                    resolved_source,
                    library_id,
                    attachment_key,
                    str(data_dir),
                    signature.size,
                    signature.mtime_ns,
                    problem_status,
                    reason,
                    now,
                    now,
                    _json_or_none(metadata),
                ),
            )
            row = connection.execute(
                "select * from problem_documents where source_path = ?",
                (resolved_source,),
            ).fetchone()
        return _row_dict(row) or {}

    def list_problem_documents(
        self,
        *,
        statuses: set[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = "where resolved_at is null"
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            where += f" and problem_status in ({placeholders})"
            params.extend(sorted(statuses))
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                select *
                from problem_documents
                {where}
                order by last_seen_at desc
                limit ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def resolve_problem_document(
        self,
        *,
        source_path: Path,
        reason: str,
    ) -> dict[str, Any] | None:
        now = _utc_now().isoformat()
        resolved_source = str(source_path.resolve())
        with self._connect() as connection:
            connection.execute(
                """
                update problem_documents
                set resolved_at = ?,
                    reason = ?,
                    last_seen_at = ?
                where source_path = ?
                  and resolved_at is null
                """,
                (now, reason, now, resolved_source),
            )
            row = connection.execute(
                "select * from problem_documents where source_path = ?",
                (resolved_source,),
            ).fetchone()
        return _row_dict(row)

    def problem_summary(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                select problem_status, count(*) as count
                from problem_documents
                where resolved_at is null
                group by problem_status
                order by problem_status
                """
            ).fetchall()
        counts = {str(row["problem_status"]): int(row["count"]) for row in rows}
        return {
            "total": sum(counts.values()),
            "blank_or_empty": counts.get("blank_or_empty", 0),
            "unreadable": counts.get("unreadable", 0),
            "stale_source": counts.get("stale_source", 0),
            "counts": counts,
        }

    def enqueue_job(
        self,
        *,
        library_id: str,
        attachment_key: str,
        data_dir: Path,
        source_path: Path,
        signature: FileSignature,
        status: str,
        reason: str,
        force: bool = False,
        max_attempts: int = 3,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        now = _utc_now()
        dedupe_key = _job_dedupe_key(
            library_id=library_id,
            attachment_key=attachment_key,
            signature=signature,
            force=force,
        )
        existing = self.get_job_by_dedupe_key(dedupe_key)
        if existing is not None:
            return {**existing, "created": False}

        job_id = f"job_{now.strftime('%Y%m%dT%H%M%S%fZ')}_{attachment_key}"
        with self._connect() as connection:
            connection.execute(
                """
                insert into ocr_jobs (
                  job_id,
                  dedupe_key,
                  library_id,
                  attachment_key,
                  data_dir,
                  source_path,
                  source_size,
                  source_mtime_ns,
                  status,
                  reason,
                  force,
                  attempts,
                  max_attempts,
                  phase,
                  last_error,
                  created_at,
                  updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    dedupe_key,
                    library_id,
                    attachment_key,
                    str(data_dir),
                    str(source_path),
                    signature.size,
                    signature.mtime_ns,
                    status,
                    reason,
                    1 if force else 0,
                    max_attempts,
                    "queued" if status == "queued" else status,
                    last_error,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            self._add_job_event(
                connection,
                job_id=job_id,
                event="created",
                message=f"Job created with status {status}: {reason}",
            )
        return {**(self.get_job(job_id) or {}), "created": True}

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "select * from ocr_jobs where job_id = ?",
                (job_id,),
            ).fetchone()
        return _row_dict(row)

    def get_job_by_dedupe_key(self, dedupe_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "select * from ocr_jobs where dedupe_key = ?",
                (dedupe_key,),
            ).fetchone()
        return _row_dict(row)

    def list_jobs(
        self,
        *,
        statuses: set[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            where = f"where status in ({placeholders})"
            params.extend(sorted(statuses))
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                select *
                from ocr_jobs
                {where}
                order by created_at asc
                limit ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def queue_summary(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                select status, count(*) as count
                from ocr_jobs
                group by status
                order by status
                """
            ).fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return {
            "queued": counts.get("queued", 0),
            "running": counts.get("running", 0),
            "succeeded": counts.get("succeeded", 0),
            "failed_retryable": counts.get("failed_retryable", 0),
            "failed_final": counts.get("failed_final", 0),
            "problem_document": counts.get("problem_document", 0),
            "manual_review": counts.get("manual_review", 0),
            "legacy_manual_review": counts.get("manual_review", 0),
            "cancelled": counts.get("cancelled", 0),
            "counts": counts,
            "problem_documents": self.problem_summary(),
        }

    def enqueue_html_job(
        self,
        *,
        library_id: str,
        attachment_key: str,
        data_dir: Path,
        source_path: Path,
        signature: FileSignature,
        collection_key: str,
        status: str,
        reason: str,
        force: bool = False,
        pipeline_key: str = "default",
        max_attempts: int = 3,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        now = _utc_now()
        dedupe_key = _html_job_dedupe_key(
            library_id=library_id,
            attachment_key=attachment_key,
            signature=signature,
            force=force,
            pipeline_key=pipeline_key,
        )
        existing = self.get_html_job_by_dedupe_key(dedupe_key)
        if existing is not None:
            return {**existing, "created": False}

        job_id = f"html_{now.strftime('%Y%m%dT%H%M%S%fZ')}_{attachment_key}"
        with self._connect() as connection:
            connection.execute(
                """
                insert into html_jobs (
                  job_id,
                  dedupe_key,
                  library_id,
                  attachment_key,
                  data_dir,
                  source_path,
                  source_size,
                  source_mtime_ns,
                  collection_key,
                  pipeline_key,
                  status,
                  reason,
                  force,
                  attempts,
                  max_attempts,
                  phase,
                  last_error,
                  created_at,
                  updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    dedupe_key,
                    library_id,
                    attachment_key,
                    str(data_dir),
                    str(source_path),
                    signature.size,
                    signature.mtime_ns,
                    collection_key,
                    pipeline_key,
                    status,
                    reason,
                    1 if force else 0,
                    max_attempts,
                    "queued" if status == "queued" else status,
                    last_error,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            self._add_html_job_event(
                connection,
                job_id=job_id,
                event="created",
                message=f"HTML job created with status {status}: {reason}",
            )
        return {**(self.get_html_job(job_id) or {}), "created": True}

    def get_html_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "select * from html_jobs where job_id = ?",
                (job_id,),
            ).fetchone()
        return _row_dict(row)

    def get_html_job_by_dedupe_key(self, dedupe_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "select * from html_jobs where dedupe_key = ?",
                (dedupe_key,),
            ).fetchone()
        return _row_dict(row)

    def list_html_jobs(
        self,
        *,
        statuses: set[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            where = f"where status in ({placeholders})"
            params.extend(sorted(statuses))
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                select *
                from html_jobs
                {where}
                order by created_at asc
                limit ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_html_job_for_attachment(
        self,
        *,
        library_id: str,
        attachment_key: str,
        statuses: set[str] | None = None,
    ) -> dict[str, Any] | None:
        params: list[Any] = [library_id, attachment_key]
        status_clause = ""
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            status_clause = f"and status in ({placeholders})"
            params.extend(sorted(statuses))
        with self._connect() as connection:
            row = connection.execute(
                f"""
                select *
                from html_jobs
                where library_id = ?
                  and attachment_key = ?
                  {status_clause}
                order by updated_at desc, created_at desc
                limit 1
                """,
                params,
            ).fetchone()
        return _row_dict(row)

    def html_queue_summary(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                select status, count(*) as count
                from html_jobs
                group by status
                order by status
                """
            ).fetchall()
        counts = {str(row["status"]) for row in rows}
        count_values = {str(row["status"]): int(row["count"]) for row in rows}
        return {
            "queued": count_values.get("queued", 0),
            "running": count_values.get("running", 0),
            "succeeded": count_values.get("succeeded", 0),
            "failed_retryable": count_values.get("failed_retryable", 0),
            "failed_final": count_values.get("failed_final", 0),
            "needs_chunk_fallback": count_values.get("needs_chunk_fallback", 0),
            "skipped": count_values.get("skipped", 0),
            "cancelled": count_values.get("cancelled", 0),
            "counts": {status: count_values[status] for status in sorted(counts)},
        }

    def recover_expired_html_jobs(self) -> int:
        now = _utc_now().isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                """
                select job_id
                from html_jobs
                where status = 'running'
                  and leased_until is not null
                  and leased_until < ?
                """,
                (now,),
            ).fetchall()
            job_ids = [str(row["job_id"]) for row in rows]
            recovered = 0
            for job_id in job_ids:
                cursor = connection.execute(
                    """
                    update html_jobs
                    set status = 'queued',
                        phase = 'recovered',
                        lease_owner = null,
                        leased_until = null,
                        last_error = 'Recovered expired running HTML job lease.',
                        updated_at = ?
                    where job_id = ?
                      and status = 'running'
                      and leased_until is not null
                      and leased_until < ?
                    """,
                    (now, job_id, now),
                )
                if cursor.rowcount != 1:
                    continue
                recovered += 1
                self._add_html_job_event(
                    connection,
                    job_id=job_id,
                    event="recovered",
                    message="Expired running HTML job lease was returned to queued.",
                )
        return recovered

    def lease_next_html_job(self, *, owner: str, lease_seconds: int) -> dict[str, Any] | None:
        self.recover_expired_html_jobs()
        now = _utc_now()
        leased_until = now + timedelta(seconds=lease_seconds)
        with self._connect() as connection:
            connection.execute("begin immediate")
            exhausted_rows = connection.execute(
                """
                select job_id
                from html_jobs
                where status = 'queued'
                  and max_attempts > 0
                  and attempts >= max_attempts
                """
            ).fetchall()
            for exhausted_row in exhausted_rows:
                exhausted_job_id = str(exhausted_row["job_id"])
                connection.execute(
                    """
                    update html_jobs
                    set status = 'failed_final',
                        phase = 'failed',
                        last_error = coalesce(last_error, 'Attempt budget exhausted before claim.'),
                        updated_at = ?
                    where job_id = ?
                      and status = 'queued'
                      and max_attempts > 0
                      and attempts >= max_attempts
                    """,
                    (now.isoformat(), exhausted_job_id),
                )
                self._add_html_job_event(
                    connection,
                    job_id=exhausted_job_id,
                    event="attempts_exhausted",
                    message="HTML job was finalized because its attempt budget is exhausted.",
                )
            row = connection.execute(
                """
                select *
                from html_jobs
                where status = 'queued'
                  and (max_attempts <= 0 or attempts < max_attempts)
                order by created_at asc
                limit 1
                """
            ).fetchone()
            if row is None:
                return None
            job = dict(row)
            cursor = connection.execute(
                """
                update html_jobs
                set status = 'running',
                    phase = 'leased',
                    attempts = attempts + 1,
                    lease_owner = ?,
                    leased_until = ?,
                    updated_at = ?
                where job_id = ?
                  and status = 'queued'
                  and (max_attempts <= 0 or attempts < max_attempts)
                """,
                (owner, leased_until.isoformat(), now.isoformat(), job["job_id"]),
            )
            if cursor.rowcount != 1:
                return None
            self._add_html_job_event(
                connection,
                job_id=job["job_id"],
                event="leased",
                message=f"HTML job leased by {owner}.",
            )
        return self.get_html_job(str(job["job_id"]))

    def mark_html_job_succeeded(
        self,
        *,
        job_id: str,
        message: str,
        en_html_path: str | None = None,
        ru_html_path: str | None = None,
        source_language: str | None = None,
        target_language: str | None = None,
        translation_skipped_reason: str | None = None,
        relay_result: Any = None,
        owner: str | None = None,
    ) -> dict[str, Any]:
        now = _utc_now().isoformat()
        owner_clause, owner_params = _terminal_owner_clause(owner)
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                update html_jobs
                set status = 'succeeded',
                    phase = 'complete',
                    lease_owner = null,
                    leased_until = null,
                    last_error = null,
                    en_html_path = coalesce(?, en_html_path),
                    ru_html_path = coalesce(?, ru_html_path),
                    source_language = coalesce(?, source_language),
                    target_language = coalesce(?, target_language),
                    translation_skipped_reason = coalesce(?, translation_skipped_reason),
                    relay_status = ?,
                    relay_result = ?,
                    updated_at = ?
                where job_id = ?
                  {owner_clause}
                """,
                (
                    en_html_path,
                    ru_html_path,
                    source_language,
                    target_language,
                    translation_skipped_reason,
                    _html_relay_status(relay_result),
                    _json_or_none(relay_result),
                    now,
                    job_id,
                    *owner_params,
                ),
            )
            if cursor.rowcount != 1:
                self._add_html_job_event(
                    connection,
                    job_id=job_id,
                    event="stale_completion_discarded",
                    message=_stale_job_update_message("HTML", owner),
                )
            else:
                self._add_html_job_event(
                    connection, job_id=job_id, event="succeeded", message=message
                )
        return self.get_html_job(job_id) or {}

    def mark_html_job_deferred(
        self,
        *,
        job_id: str,
        status: str,
        message: str,
        metadata: Any = None,
        owner: str | None = None,
    ) -> dict[str, Any]:
        now = _utc_now().isoformat()
        owner_clause, owner_params = _terminal_owner_clause(owner)
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                update html_jobs
                set status = ?,
                    phase = 'deferred',
                    attempts = case when attempts > 0 then attempts - 1 else 0 end,
                    lease_owner = null,
                    leased_until = null,
                    last_error = ?,
                    relay_status = 'deferred',
                    relay_result = ?,
                    updated_at = ?
                where job_id = ?
                  {owner_clause}
                """,
                (status, message, _json_or_none(metadata), now, job_id, *owner_params),
            )
            if cursor.rowcount != 1:
                self._add_html_job_event(
                    connection,
                    job_id=job_id,
                    event="stale_completion_discarded",
                    message=_stale_job_update_message("HTML", owner),
                )
            else:
                self._add_html_job_event(
                    connection, job_id=job_id, event=status, message=message
                )
        return self.get_html_job(job_id) or {}

    def mark_html_job_failed(
        self,
        *,
        job_id: str,
        message: str,
        retryable: bool,
        owner: str | None = None,
    ) -> dict[str, Any]:
        job = self.get_html_job(job_id)
        if job is None:
            return {}
        attempts = int(job["attempts"])
        max_attempts = int(job["max_attempts"])
        status = (
            "failed_retryable"
            if retryable and (max_attempts <= 0 or attempts < max_attempts)
            else "failed_final"
        )
        now = _utc_now().isoformat()
        owner_clause, owner_params = _terminal_owner_clause(owner)
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                update html_jobs
                set status = ?,
                    phase = 'failed',
                    lease_owner = null,
                    leased_until = null,
                    last_error = ?,
                    updated_at = ?
                where job_id = ?
                  {owner_clause}
                """,
                (status, message, now, job_id, *owner_params),
            )
            if cursor.rowcount != 1:
                self._add_html_job_event(
                    connection,
                    job_id=job_id,
                    event="stale_completion_discarded",
                    message=_stale_job_update_message("HTML", owner),
                )
            else:
                self._add_html_job_event(
                    connection, job_id=job_id, event=status, message=message
                )
        return self.get_html_job(job_id) or {}

    def heartbeat_html_job(
        self,
        *,
        job_id: str,
        owner: str,
        lease_seconds: int,
    ) -> bool:
        now = _utc_now()
        leased_until = now + timedelta(seconds=max(1, int(lease_seconds)))
        with self._connect() as connection:
            cursor = connection.execute(
                """
                update html_jobs
                set leased_until = ?,
                    updated_at = ?
                where job_id = ?
                  and status = 'running'
                  and lease_owner = ?
                """,
                (leased_until.isoformat(), now.isoformat(), job_id, str(owner)),
            )
        return cursor.rowcount == 1

    def retry_html_job(self, job_id: str, *, reset_attempts: bool = False) -> dict[str, Any]:
        now = _utc_now().isoformat()
        with self._connect() as connection:
            connection.execute("begin immediate")
            row = connection.execute(
                "select * from html_jobs where job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                return {}
            attempts = int(row["attempts"])
            max_attempts = int(row["max_attempts"])
            if not reset_attempts and max_attempts > 0 and attempts >= max_attempts:
                self._add_html_job_event(
                    connection,
                    job_id=job_id,
                    event="retry_rejected_exhausted",
                    message=(
                        "HTML retry was rejected because the attempt budget is exhausted; "
                        "use reset_attempts=true to start a new retry generation."
                    ),
                )
                return dict(row)
            if reset_attempts:
                cursor = connection.execute(
                    """
                    update html_jobs
                    set status = 'queued',
                        phase = 'queued',
                        attempts = 0,
                        lease_owner = null,
                        leased_until = null,
                        updated_at = ?
                    where job_id = ?
                      and status in ('failed_retryable', 'failed_final', 'cancelled', 'needs_chunk_fallback')
                    """,
                    (now, job_id),
                )
                event = "retry_generation_reset"
            else:
                cursor = connection.execute(
                    """
                    update html_jobs
                    set status = 'queued',
                        phase = 'queued',
                        lease_owner = null,
                        leased_until = null,
                        updated_at = ?
                    where job_id = ?
                      and status in ('failed_retryable', 'failed_final', 'cancelled', 'needs_chunk_fallback')
                      and (max_attempts <= 0 or attempts < max_attempts)
                    """,
                    (now, job_id),
                )
                event = "retry"
            if cursor.rowcount == 1:
                self._add_html_job_event(
                    connection,
                    job_id=job_id,
                    event=event,
                    message=(
                        "HTML job started a new retry generation."
                        if reset_attempts
                        else "HTML job returned to queue."
                    ),
                )
        return self.get_html_job(job_id) or {}

    def cancel_html_job(self, job_id: str) -> dict[str, Any]:
        now = _utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                update html_jobs
                set status = 'cancelled',
                    phase = 'cancelled',
                    lease_owner = null,
                    leased_until = null,
                    updated_at = ?
                where job_id = ?
                  and status in ('queued', 'failed_retryable', 'needs_chunk_fallback')
                """,
                (now, job_id),
            )
            self._add_html_job_event(
                connection,
                job_id=job_id,
                event="cancelled",
                message="HTML job cancelled.",
            )
        return self.get_html_job(job_id) or {}

    def enqueue_metadata_job(
        self,
        *,
        job_type: str,
        library_id: str,
        attachment_key: str,
        data_dir: Path,
        source_path: Path,
        signature: FileSignature,
        status: str,
        reason: str,
        force: bool = False,
        parent_item_key: str | None = None,
        parent_version: int | None = None,
        queue_key: str = "default",
        max_attempts: int = 3,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        now = _utc_now()
        dedupe_key = _metadata_job_dedupe_key(
            job_type=job_type,
            library_id=library_id,
            attachment_key=attachment_key,
            signature=signature,
            force=force,
            queue_key=queue_key,
        )
        existing = self.get_metadata_job_by_dedupe_key(dedupe_key)
        if existing is not None:
            return {**existing, "created": False}

        safe_job_type = _safe_job_type(job_type)
        job_id = f"meta_{safe_job_type}_{now.strftime('%Y%m%dT%H%M%S%fZ')}_{attachment_key}"
        with self._connect() as connection:
            connection.execute(
                """
                insert into metadata_jobs (
                  job_id,
                  job_type,
                  dedupe_key,
                  library_id,
                  attachment_key,
                  data_dir,
                  source_path,
                  source_size,
                  source_mtime_ns,
                  parent_item_key,
                  parent_version,
                  queue_key,
                  status,
                  reason,
                  force,
                  attempts,
                  max_attempts,
                  phase,
                  last_error,
                  created_at,
                  updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    job_type,
                    dedupe_key,
                    library_id,
                    attachment_key,
                    str(data_dir),
                    str(source_path),
                    signature.size,
                    signature.mtime_ns,
                    parent_item_key,
                    parent_version,
                    queue_key,
                    status,
                    reason,
                    1 if force else 0,
                    max_attempts,
                    "queued" if status == "queued" else status,
                    last_error,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            self._add_metadata_job_event(
                connection,
                job_id=job_id,
                event="created",
                message=f"Metadata job created with status {status}: {reason}",
            )
        return {**(self.get_metadata_job(job_id) or {}), "created": True}

    def get_metadata_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "select * from metadata_jobs where job_id = ?",
                (job_id,),
            ).fetchone()
        return _row_dict(row)

    def get_metadata_job_by_dedupe_key(self, dedupe_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "select * from metadata_jobs where dedupe_key = ?",
                (dedupe_key,),
            ).fetchone()
        return _row_dict(row)

    def get_metadata_job_by_parent_scope(
        self,
        *,
        job_type: str,
        library_id: str,
        parent_item_key: str,
        parent_version: int | None,
        queue_key: str,
        force: bool = False,
        statuses: set[str] | None = None,
    ) -> dict[str, Any] | None:
        params: list[Any] = [
            job_type,
            library_id,
            parent_item_key,
            queue_key,
            1 if force else 0,
        ]
        if parent_version is None:
            version_clause = "and parent_version is null"
        else:
            version_clause = "and parent_version = ?"
            params.append(parent_version)
        status_clause = ""
        if statuses:
            status_clause = f" and status in ({','.join('?' for _ in statuses)})"
            params.extend(sorted(statuses))

        with self._connect() as connection:
            row = connection.execute(
                f"""
                select *
                from metadata_jobs
                where job_type = ?
                  and library_id = ?
                  and parent_item_key = ?
                  and queue_key = ?
                  and force = ?
                  {version_clause}
                  {status_clause}
                order by
                  case status
                    when 'queued' then 0
                    when 'running' then 1
                    when 'failed_retryable' then 2
                    when 'succeeded' then 3
                    when 'failed_final' then 4
                    else 5
                  end,
                  created_at desc
                limit 1
                """,
                params,
            ).fetchone()
        return _row_dict(row)

    def list_metadata_jobs(
        self,
        *,
        job_type: str | None = None,
        statuses: set[str] | None = None,
        limit: int | None = 100,
        library_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        clauses: list[str] = []
        if job_type:
            clauses.append("job_type = ?")
            params.append(job_type)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status in ({placeholders})")
            params.extend(sorted(statuses))
        scoped_library_ids = sorted(
            {str(value).strip() for value in (library_ids or set()) if str(value).strip()}
        )
        if scoped_library_ids:
            clauses.append(f"library_id in ({','.join('?' for _ in scoped_library_ids)})")
            params.extend(scoped_library_ids)
        where = f"where {' and '.join(clauses)}" if clauses else ""
        limit_clause = "" if limit is None else "limit ?"
        if limit is not None:
            params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                select *
                from metadata_jobs
                {where}
                order by created_at asc
                {limit_clause}
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def metadata_queue_summary(
        self,
        *,
        job_type: str | None = None,
        library_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        params: list[Any] = []
        clauses: list[str] = []
        if job_type:
            clauses.append("job_type = ?")
            params.append(job_type)
        scoped_library_ids = sorted(
            {str(value).strip() for value in (library_ids or set()) if str(value).strip()}
        )
        if scoped_library_ids:
            clauses.append(f"library_id in ({','.join('?' for _ in scoped_library_ids)})")
            params.extend(scoped_library_ids)
        where = f"where {' and '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                select status, count(*) as count
                from metadata_jobs
                {where}
                group by status
                order by status
                """,
                params,
            ).fetchall()
            failed_where = " and ".join(("status = 'failed_final'", *clauses))
            transient_rows = connection.execute(
                f"""
                select last_error
                from metadata_jobs
                where {failed_where}
                """,
                params,
            ).fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        failed_transient = sum(
            1
            for row in transient_rows
            if _metadata_failure_is_transient(str(row["last_error"] or ""))
        )
        return {
            "job_type": job_type,
            "library_ids": scoped_library_ids or None,
            "queued": counts.get("queued", 0),
            "running": counts.get("running", 0),
            "succeeded": counts.get("succeeded", 0),
            "skipped": counts.get("skipped", 0),
            "failed_retryable": counts.get("failed_retryable", 0),
            "failed_final": counts.get("failed_final", 0),
            "failed_transient": failed_transient,
            "cancelled": counts.get("cancelled", 0),
            "counts": counts,
        }

    def recover_expired_metadata_jobs(self, *, job_type: str | None = None) -> int:
        now = _utc_now().isoformat()
        params: list[Any] = [now]
        type_clause = ""
        if job_type:
            type_clause = "and job_type = ?"
            params.append(job_type)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                select job_id
                from metadata_jobs
                where status = 'running'
                  and leased_until is not null
                  and leased_until < ?
                  {type_clause}
                """,
                params,
            ).fetchall()
            job_ids = [str(row["job_id"]) for row in rows]
            recovered = 0
            for job_id in job_ids:
                cursor = connection.execute(
                    """
                    update metadata_jobs
                    set status = 'queued',
                        phase = 'recovered',
                        lease_owner = null,
                        leased_until = null,
                        last_error = 'Recovered expired metadata job lease.',
                        updated_at = ?
                    where job_id = ?
                      and status = 'running'
                      and leased_until is not null
                      and leased_until < ?
                    """,
                    (now, job_id, now),
                )
                if cursor.rowcount != 1:
                    continue
                recovered += 1
                self._add_metadata_job_event(
                    connection,
                    job_id=job_id,
                    event="recovered",
                    message="Expired running metadata job lease was returned to queued.",
                )
        return recovered

    def recover_orphaned_metadata_jobs(
        self,
        *,
        job_type: str | None = None,
        owner_alive: Callable[[str], bool] | None = None,
    ) -> int:
        owner_alive = owner_alive or _lease_owner_process_alive
        now = _utc_now().isoformat()
        params: list[Any] = []
        type_clause = ""
        if job_type:
            type_clause = "and job_type = ?"
            params.append(job_type)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                select job_id, lease_owner
                from metadata_jobs
                where status = 'running'
                  and lease_owner is not null
                  {type_clause}
                """,
                params,
            ).fetchall()
            jobs = [
                (str(row["job_id"]), str(row["lease_owner"] or ""))
                for row in rows
                if not owner_alive(str(row["lease_owner"] or ""))
            ]
            recovered = 0
            for job_id, previous_owner in jobs:
                cursor = connection.execute(
                    """
                    update metadata_jobs
                    set status = 'queued',
                        phase = 'recovered',
                        lease_owner = null,
                        leased_until = null,
                        last_error = 'Recovered orphaned metadata job lease.',
                        updated_at = ?
                    where job_id = ?
                      and status = 'running'
                      and lease_owner = ?
                    """,
                    (now, job_id, previous_owner),
                )
                if cursor.rowcount != 1:
                    continue
                recovered += 1
                self._add_metadata_job_event(
                    connection,
                    job_id=job_id,
                    event="recovered",
                    message="Orphaned running metadata job lease was returned to queued.",
                )
        return recovered

    def lease_next_metadata_job(
        self,
        *,
        job_type: str,
        owner: str,
        lease_seconds: int,
    ) -> dict[str, Any] | None:
        self.recover_expired_metadata_jobs(job_type=job_type)
        now = _utc_now()
        leased_until = now + timedelta(seconds=lease_seconds)
        with self._connect() as connection:
            connection.execute("begin immediate")
            exhausted_rows = connection.execute(
                """
                select job_id
                from metadata_jobs
                where job_type = ?
                  and status = 'queued'
                  and max_attempts > 0
                  and attempts >= max_attempts
                """,
                (job_type,),
            ).fetchall()
            for exhausted_row in exhausted_rows:
                exhausted_job_id = str(exhausted_row["job_id"])
                connection.execute(
                    """
                    update metadata_jobs
                    set status = 'failed_final',
                        phase = 'failed',
                        last_error = coalesce(last_error, 'Attempt budget exhausted before claim.'),
                        updated_at = ?
                    where job_id = ?
                      and status = 'queued'
                      and max_attempts > 0
                      and attempts >= max_attempts
                    """,
                    (now.isoformat(), exhausted_job_id),
                )
                self._add_metadata_job_event(
                    connection,
                    job_id=exhausted_job_id,
                    event="attempts_exhausted",
                    message="Metadata job was finalized because its attempt budget is exhausted.",
                )
            row = connection.execute(
                """
                select *
                from metadata_jobs
                where job_type = ?
                  and status = 'queued'
                  and (max_attempts <= 0 or attempts < max_attempts)
                order by created_at asc
                limit 1
                """,
                (job_type,),
            ).fetchone()
            if row is None:
                return None
            job = dict(row)
            cursor = connection.execute(
                """
                update metadata_jobs
                set status = 'running',
                    phase = 'leased',
                    attempts = attempts + 1,
                    lease_owner = ?,
                    leased_until = ?,
                    updated_at = ?
                where job_id = ?
                  and status = 'queued'
                  and (max_attempts <= 0 or attempts < max_attempts)
                """,
                (owner, leased_until.isoformat(), now.isoformat(), job["job_id"]),
            )
            if cursor.rowcount != 1:
                return None
            self._add_metadata_job_event(
                connection,
                job_id=job["job_id"],
                event="leased",
                message=f"Metadata job leased by {owner}.",
            )
        return self.get_metadata_job(str(job["job_id"]))

    def mark_metadata_job_succeeded(
        self,
        *,
        job_id: str,
        message: str,
        result: Any = None,
        output_path: str | None = None,
        relay_result: Any = None,
        owner: str | None = None,
    ) -> dict[str, Any]:
        now = _utc_now().isoformat()
        owner_clause, owner_params = _terminal_owner_clause(owner)
        result_json = _json_or_none(result)
        stored_result_json = _compact_metadata_result_json(result_json)
        completed = False
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                update metadata_jobs
                set status = 'succeeded',
                    phase = 'complete',
                    lease_owner = null,
                    leased_until = null,
                    last_error = null,
                    result_json = ?,
                    output_path = coalesce(?, output_path),
                    relay_status = ?,
                    relay_result = ?,
                    updated_at = ?
                where job_id = ?
                  {owner_clause}
                """,
                (
                    stored_result_json,
                    output_path,
                    _metadata_relay_status(relay_result),
                    _json_or_none(relay_result),
                    now,
                    job_id,
                    *owner_params,
                ),
            )
            if cursor.rowcount != 1:
                self._add_metadata_job_event(
                    connection,
                    job_id=job_id,
                    event="stale_completion_discarded",
                    message=_stale_job_update_message("metadata", owner),
                )
            else:
                completed = True
                self._add_metadata_job_event(
                    connection, job_id=job_id, event="succeeded", message=message
                )
        job = self.get_metadata_job(job_id) or {}
        if completed and result_json != stored_result_json:
            job["result_json"] = result_json
        return job

    def mark_metadata_job_skipped(
        self,
        *,
        job_id: str,
        message: str,
        result: Any = None,
        owner: str | None = None,
    ) -> dict[str, Any]:
        now = _utc_now().isoformat()
        owner_clause, owner_params = _terminal_owner_clause(owner)
        result_json = _json_or_none(result)
        stored_result_json = _compact_metadata_result_json(result_json)
        completed = False
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                update metadata_jobs
                set status = 'skipped',
                    phase = 'skipped',
                    lease_owner = null,
                    leased_until = null,
                    last_error = ?,
                    result_json = ?,
                    relay_status = 'skipped',
                    updated_at = ?
                where job_id = ?
                  {owner_clause}
                """,
                (message, stored_result_json, now, job_id, *owner_params),
            )
            if cursor.rowcount != 1:
                self._add_metadata_job_event(
                    connection,
                    job_id=job_id,
                    event="stale_completion_discarded",
                    message=_stale_job_update_message("metadata", owner),
                )
            else:
                completed = True
                self._add_metadata_job_event(
                    connection, job_id=job_id, event="skipped", message=message
                )
        job = self.get_metadata_job(job_id) or {}
        if completed and result_json != stored_result_json:
            job["result_json"] = result_json
        return job

    def mark_metadata_job_failed(
        self,
        *,
        job_id: str,
        message: str,
        retryable: bool,
        owner: str | None = None,
    ) -> dict[str, Any]:
        job = self.get_metadata_job(job_id)
        if job is None:
            return {}
        attempts = int(job["attempts"])
        max_attempts = int(job["max_attempts"])
        status = (
            "failed_retryable"
            if retryable and (max_attempts <= 0 or attempts < max_attempts)
            else "failed_final"
        )
        now = _utc_now().isoformat()
        owner_clause, owner_params = _terminal_owner_clause(owner)
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                update metadata_jobs
                set status = ?,
                    phase = 'failed',
                    lease_owner = null,
                    leased_until = null,
                    last_error = ?,
                    updated_at = ?
                where job_id = ?
                  {owner_clause}
                """,
                (status, message, now, job_id, *owner_params),
            )
            if cursor.rowcount != 1:
                self._add_metadata_job_event(
                    connection,
                    job_id=job_id,
                    event="stale_completion_discarded",
                    message=_stale_job_update_message("metadata", owner),
                )
            else:
                self._add_metadata_job_event(
                    connection, job_id=job_id, event=status, message=message
                )
        return self.get_metadata_job(job_id) or {}

    def heartbeat_metadata_job(
        self,
        *,
        job_id: str,
        owner: str,
        lease_seconds: int,
    ) -> bool:
        now = _utc_now()
        leased_until = now + timedelta(seconds=max(1, int(lease_seconds)))
        with self._connect() as connection:
            cursor = connection.execute(
                """
                update metadata_jobs
                set leased_until = ?,
                    updated_at = ?
                where job_id = ?
                  and status = 'running'
                  and lease_owner = ?
                """,
                (leased_until.isoformat(), now.isoformat(), job_id, str(owner)),
            )
        return cursor.rowcount == 1

    def retry_metadata_job(self, job_id: str, *, reset_attempts: bool = False) -> dict[str, Any]:
        now = _utc_now().isoformat()
        with self._connect() as connection:
            connection.execute("begin immediate")
            row = connection.execute(
                """
                select *
                from metadata_jobs
                where job_id = ?
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                return {}
            attempts = int(row["attempts"])
            max_attempts = int(row["max_attempts"])
            if not reset_attempts and max_attempts > 0 and attempts >= max_attempts:
                self._add_metadata_job_event(
                    connection,
                    job_id=job_id,
                    event="retry_rejected_exhausted",
                    message=(
                        "Metadata retry was rejected because the attempt budget is exhausted; "
                        "use reset_attempts=true to start a new retry generation."
                    ),
                )
                return dict(row)
            if reset_attempts:
                cursor = connection.execute(
                    """
                    update metadata_jobs
                    set status = 'queued',
                        phase = 'queued',
                        attempts = 0,
                        lease_owner = null,
                        leased_until = null,
                        updated_at = ?
                    where job_id = ?
                      and status in ('failed_retryable', 'failed_final', 'skipped', 'cancelled')
                    """,
                    (now, job_id),
                )
                event = "retry_generation_reset"
            else:
                cursor = connection.execute(
                    """
                    update metadata_jobs
                    set status = 'queued',
                        phase = 'queued',
                        lease_owner = null,
                        leased_until = null,
                        updated_at = ?
                    where job_id = ?
                      and status in ('failed_retryable', 'failed_final', 'skipped', 'cancelled')
                      and (max_attempts <= 0 or attempts < max_attempts)
                    """,
                    (now, job_id),
                )
                event = "retry"
            if cursor.rowcount == 1:
                self._add_metadata_job_event(
                    connection,
                    job_id=job_id,
                    event=event,
                    message=(
                        "Metadata job started a new retry generation."
                        if reset_attempts
                        else "Metadata job returned to queue."
                    ),
                )
        return self.get_metadata_job(job_id) or {}

    def cancel_metadata_job(self, job_id: str) -> dict[str, Any]:
        now = _utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                update metadata_jobs
                set status = 'cancelled',
                    phase = 'cancelled',
                    lease_owner = null,
                    leased_until = null,
                    updated_at = ?
                where job_id = ?
                  and status in ('queued', 'failed_retryable', 'skipped')
                """,
                (now, job_id),
            )
            self._add_metadata_job_event(
                connection,
                job_id=job_id,
                event="cancelled",
                message="Metadata job cancelled.",
            )
        return self.get_metadata_job(job_id) or {}

    def recover_expired_jobs(self) -> int:
        now = _utc_now().isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                """
                select job_id
                from ocr_jobs
                where status = 'running'
                  and leased_until is not null
                  and leased_until < ?
                """,
                (now,),
            ).fetchall()
            job_ids = [str(row["job_id"]) for row in rows]
            recovered = 0
            for job_id in job_ids:
                cursor = connection.execute(
                    """
                    update ocr_jobs
                    set status = 'queued',
                        phase = 'recovered',
                        lease_owner = null,
                        leased_until = null,
                        last_error = 'Recovered expired running job lease.',
                        updated_at = ?
                    where job_id = ?
                      and status = 'running'
                      and leased_until is not null
                      and leased_until < ?
                    """,
                    (now, job_id, now),
                )
                if cursor.rowcount != 1:
                    continue
                recovered += 1
                self._add_job_event(
                    connection,
                    job_id=job_id,
                    event="recovered",
                    message="Expired running job lease was returned to queued.",
                )
        return recovered

    def lease_next_job(self, *, owner: str, lease_seconds: int) -> dict[str, Any] | None:
        self.recover_expired_jobs()
        now = _utc_now()
        leased_until = now + timedelta(seconds=lease_seconds)
        with self._connect() as connection:
            connection.execute("begin immediate")
            exhausted_rows = connection.execute(
                """
                select job_id
                from ocr_jobs
                where status = 'queued'
                  and max_attempts > 0
                  and attempts >= max_attempts
                """
            ).fetchall()
            for exhausted_row in exhausted_rows:
                exhausted_job_id = str(exhausted_row["job_id"])
                connection.execute(
                    """
                    update ocr_jobs
                    set status = 'failed_final',
                        phase = 'failed',
                        last_error = coalesce(last_error, 'Attempt budget exhausted before claim.'),
                        updated_at = ?
                    where job_id = ?
                      and status = 'queued'
                      and max_attempts > 0
                      and attempts >= max_attempts
                    """,
                    (now.isoformat(), exhausted_job_id),
                )
                self._add_job_event(
                    connection,
                    job_id=exhausted_job_id,
                    event="attempts_exhausted",
                    message="OCR job was finalized because its attempt budget is exhausted.",
                )
            row = connection.execute(
                """
                select *
                from ocr_jobs
                where status = 'queued'
                  and (max_attempts <= 0 or attempts < max_attempts)
                order by created_at asc
                limit 1
                """
            ).fetchone()
            if row is None:
                return None
            job = dict(row)
            cursor = connection.execute(
                """
                update ocr_jobs
                set status = 'running',
                    phase = 'leased',
                    attempts = attempts + 1,
                    lease_owner = ?,
                    leased_until = ?,
                    updated_at = ?
                where job_id = ?
                  and status = 'queued'
                  and (max_attempts <= 0 or attempts < max_attempts)
                """,
                (owner, leased_until.isoformat(), now.isoformat(), job["job_id"]),
            )
            if cursor.rowcount != 1:
                return None
            self._add_job_event(
                connection,
                job_id=job["job_id"],
                event="leased",
                message=f"Job leased by {owner}.",
            )
        return self.get_job(str(job["job_id"]))

    def mark_job_succeeded(
        self,
        *,
        job_id: str,
        message: str,
        result_path: str | None = None,
        backup_path: str | None = None,
        relay_result: Any = None,
        owner: str | None = None,
    ) -> dict[str, Any]:
        now = _utc_now().isoformat()
        owner_clause, owner_params = _terminal_owner_clause(owner)
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                update ocr_jobs
                set status = 'succeeded',
                    phase = 'complete',
                    lease_owner = null,
                    leased_until = null,
                    last_error = null,
                    result_path = coalesce(?, result_path),
                    backup_path = coalesce(?, backup_path),
                    relay_status = ?,
                    relay_result = ?,
                    updated_at = ?
                where job_id = ?
                  {owner_clause}
                """,
                (
                    result_path,
                    backup_path,
                    "succeeded" if relay_result is not None else None,
                    _json_or_none(relay_result),
                    now,
                    job_id,
                    *owner_params,
                ),
            )
            if cursor.rowcount != 1:
                self._add_job_event(
                    connection,
                    job_id=job_id,
                    event="stale_completion_discarded",
                    message=_stale_job_update_message("OCR", owner),
                )
            else:
                self._add_job_event(
                    connection, job_id=job_id, event="succeeded", message=message
                )
        return self.get_job(job_id) or {}

    def mark_job_failed(
        self,
        *,
        job_id: str,
        message: str,
        retryable: bool,
        owner: str | None = None,
    ) -> dict[str, Any]:
        job = self.get_job(job_id)
        if job is None:
            return {}
        attempts = int(job["attempts"])
        max_attempts = int(job["max_attempts"])
        status = (
            "failed_retryable"
            if retryable and (max_attempts <= 0 or attempts < max_attempts)
            else "failed_final"
        )
        now = _utc_now().isoformat()
        owner_clause, owner_params = _terminal_owner_clause(owner)
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                update ocr_jobs
                set status = ?,
                    phase = 'failed',
                    lease_owner = null,
                    leased_until = null,
                    last_error = ?,
                    updated_at = ?
                where job_id = ?
                  {owner_clause}
                """,
                (status, message, now, job_id, *owner_params),
            )
            if cursor.rowcount != 1:
                self._add_job_event(
                    connection,
                    job_id=job_id,
                    event="stale_completion_discarded",
                    message=_stale_job_update_message("OCR", owner),
                )
            else:
                self._add_job_event(
                    connection, job_id=job_id, event=status, message=message
                )
        return self.get_job(job_id) or {}

    def mark_job_manual_review(
        self,
        *,
        job_id: str,
        message: str,
        owner: str | None = None,
    ) -> dict[str, Any]:
        now = _utc_now().isoformat()
        owner_clause, owner_params = _terminal_owner_clause(owner)
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                update ocr_jobs
                set status = 'manual_review',
                    phase = 'manual_review',
                    lease_owner = null,
                    leased_until = null,
                    last_error = ?,
                    updated_at = ?
                where job_id = ?
                  {owner_clause}
                """,
                (message, now, job_id, *owner_params),
            )
            if cursor.rowcount != 1:
                self._add_job_event(
                    connection,
                    job_id=job_id,
                    event="stale_completion_discarded",
                    message=_stale_job_update_message("OCR", owner),
                )
            else:
                self._add_job_event(
                    connection, job_id=job_id, event="manual_review", message=message
                )
        return self.get_job(job_id) or {}

    def mark_job_problem_document(
        self,
        *,
        job_id: str,
        message: str,
        owner: str | None = None,
    ) -> dict[str, Any]:
        now = _utc_now().isoformat()
        owner_clause, owner_params = _terminal_owner_clause(owner)
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                update ocr_jobs
                set status = 'problem_document',
                    phase = 'problem_document',
                    lease_owner = null,
                    leased_until = null,
                    last_error = ?,
                    updated_at = ?
                where job_id = ?
                  {owner_clause}
                """,
                (message, now, job_id, *owner_params),
            )
            if cursor.rowcount != 1:
                self._add_job_event(
                    connection,
                    job_id=job_id,
                    event="stale_completion_discarded",
                    message=_stale_job_update_message("OCR", owner),
                )
            else:
                self._add_job_event(
                    connection, job_id=job_id, event="problem_document", message=message
                )
        return self.get_job(job_id) or {}

    def retry_job(self, job_id: str, *, reset_attempts: bool = False) -> dict[str, Any]:
        now = _utc_now().isoformat()
        with self._connect() as connection:
            connection.execute("begin immediate")
            row = connection.execute(
                "select * from ocr_jobs where job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                return {}
            attempts = int(row["attempts"])
            max_attempts = int(row["max_attempts"])
            if not reset_attempts and max_attempts > 0 and attempts >= max_attempts:
                self._add_job_event(
                    connection,
                    job_id=job_id,
                    event="retry_rejected_exhausted",
                    message=(
                        "OCR retry was rejected because the attempt budget is exhausted; "
                        "use reset_attempts=true to start a new retry generation."
                    ),
                )
                return dict(row)
            if reset_attempts:
                cursor = connection.execute(
                    """
                    update ocr_jobs
                    set status = 'queued',
                        phase = 'queued',
                        attempts = 0,
                        lease_owner = null,
                        leased_until = null,
                        updated_at = ?
                    where job_id = ?
                      and status in ('failed_retryable', 'failed_final', 'manual_review', 'problem_document', 'cancelled')
                    """,
                    (now, job_id),
                )
                event = "retry_generation_reset"
            else:
                cursor = connection.execute(
                    """
                    update ocr_jobs
                    set status = 'queued',
                        phase = 'queued',
                        lease_owner = null,
                        leased_until = null,
                        updated_at = ?
                    where job_id = ?
                      and status in ('failed_retryable', 'failed_final', 'manual_review', 'problem_document', 'cancelled')
                      and (max_attempts <= 0 or attempts < max_attempts)
                    """,
                    (now, job_id),
                )
                event = "retry"
            if cursor.rowcount == 1:
                self._add_job_event(
                    connection,
                    job_id=job_id,
                    event=event,
                    message=(
                        "OCR job started a new retry generation."
                        if reset_attempts
                        else "OCR job returned to queue."
                    ),
                )
        return self.get_job(job_id) or {}

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        now = _utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                update ocr_jobs
                set status = 'cancelled',
                    phase = 'cancelled',
                    lease_owner = null,
                    leased_until = null,
                    updated_at = ?
                where job_id = ?
                  and status in ('queued', 'failed_retryable', 'manual_review', 'problem_document')
                """,
                (now, job_id),
            )
            self._add_job_event(connection, job_id=job_id, event="cancelled", message="Job cancelled.")
        return self.get_job(job_id) or {}

    def mark_job_progress(
        self,
        *,
        job_id: str,
        phase: str,
        message: str,
        owner: str | None = None,
        lease_seconds: int | None = None,
    ) -> dict[str, Any]:
        now = _utc_now()
        leased_until = (
            now + timedelta(seconds=max(1, int(lease_seconds)))
            if lease_seconds is not None
            else None
        )
        normalized_owner = str(owner or "").strip()
        owner_clause = "and status = 'running' and lease_owner = ?" if normalized_owner else ""
        owner_params = (normalized_owner,) if normalized_owner else ()
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                update ocr_jobs
                set phase = ?,
                    leased_until = coalesce(?, leased_until),
                    updated_at = ?
                where job_id = ?
                  {owner_clause}
                """,
                (
                    phase,
                    leased_until.isoformat() if leased_until else None,
                    now.isoformat(),
                    job_id,
                    *owner_params,
                ),
            )
            if cursor.rowcount != 1:
                self._add_job_event(
                    connection,
                    job_id=job_id,
                    event="stale_progress_discarded",
                    message=_stale_job_update_message("OCR", owner),
                )
            else:
                self._add_job_event(
                    connection, job_id=job_id, event="progress", message=message
                )
        return self.get_job(job_id) or {}

    def create_full_run(self, *, options: dict[str, Any]) -> dict[str, Any]:
        now = _utc_now()
        run_id = f"full_{now.strftime('%Y%m%dT%H%M%S%fZ')}"
        order_mode = str(options.get("order") or options.get("mode") or "ingest")
        with self._connect() as connection:
            connection.execute(
                """
                insert into full_runs (
                  run_id,
                  status,
                  phase,
                  order_mode,
                  options,
                  stop_requested,
                  started_at,
                  updated_at,
                  heartbeat_at
                )
                values (?, 'running', 'starting', ?, ?, 0, ?, ?, ?)
                """,
                (
                    run_id,
                    order_mode,
                    _json_or_none(options),
                    now.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            self._add_full_run_event(
                connection,
                run_id=run_id,
                event="started",
                message=f"Full run started with order {order_mode}.",
                metadata=options,
            )
        return self.get_full_run(run_id) or {}

    def get_full_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "select * from full_runs where run_id = ?",
                (run_id,),
            ).fetchone()
        return _row_dict(row)

    def latest_full_run(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                select *
                from full_runs
                order by started_at desc
                limit 1
                """
            ).fetchone()
        return _row_dict(row)

    def running_full_run(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                select *
                from full_runs
                where status in ('running', 'stopping')
                order by started_at desc
                limit 1
                """
            ).fetchone()
        return _row_dict(row)

    def update_full_run(
        self,
        *,
        run_id: str,
        status: str | None = None,
        phase: str | None = None,
        current_job_kind: str | None = None,
        current_job_id: str | None = None,
        last_error: str | None = None,
        finished: bool = False,
        event: str | None = None,
        message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                update full_runs
                set status = coalesce(?, status),
                    phase = coalesce(?, phase),
                    current_job_kind = ?,
                    current_job_id = ?,
                    last_error = coalesce(?, last_error),
                    finished_at = case when ? then ? else finished_at end,
                    heartbeat_at = ?,
                    updated_at = ?
                where run_id = ?
                """,
                (
                    status,
                    phase,
                    current_job_kind,
                    current_job_id,
                    last_error,
                    1 if finished else 0,
                    now,
                    now,
                    now,
                    run_id,
                ),
            )
            if event:
                self._add_full_run_event(
                    connection,
                    run_id=run_id,
                    event=event,
                    message=message or event,
                    metadata=metadata,
                )
        return self.get_full_run(run_id) or {}

    def request_full_run_stop(self, run_id: str) -> dict[str, Any]:
        now = _utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                update full_runs
                set stop_requested = 1,
                    status = case when status = 'running' then 'stopping' else status end,
                    updated_at = ?,
                    heartbeat_at = ?
                where run_id = ?
                """,
                (now, now, run_id),
            )
            self._add_full_run_event(
                connection,
                run_id=run_id,
                event="stop_requested",
                message="Stop requested. Current leased job is allowed to finish.",
                metadata=None,
            )
        return self.get_full_run(run_id) or {}

    def full_run_stop_requested(self, run_id: str) -> bool:
        row = self.get_full_run(run_id)
        return bool(row and int(row.get("stop_requested") or 0))

    def list_full_run_events(self, run_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                select *
                from full_run_events
                where run_id = ?
                order by event_id desc
                limit ?
                """,
                (run_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def prune_event_history(
        self,
        *,
        retention_days: int = _EVENT_RETENTION_DAYS,
        keep_per_entity: int = _EVENT_KEEP_PER_ENTITY,
        batch_size: int = _EVENT_PRUNE_BATCH_SIZE,
        max_batches: int = 20,
    ) -> dict[str, Any]:
        retention_days = max(1, int(retention_days))
        keep_per_entity = max(0, int(keep_per_entity))
        batch_size = min(10_000, max(1, int(batch_size)))
        max_batches = min(100, max(1, int(max_batches)))
        cutoff = (_utc_now() - timedelta(days=retention_days)).isoformat()
        result: dict[str, Any] = {
            "ok": True,
            "mode": "prune_event_history",
            "retention_days": retention_days,
            "keep_per_entity": keep_per_entity,
            "batch_size": batch_size,
            "max_batches": max_batches,
            "cutoff": cutoff,
        }

        with self._connect() as connection:
            tables = _existing_event_tables(connection)
            result["counts_before"] = _event_table_counts(connection, tables)
            deleted, has_more = _prune_event_tables(
                connection,
                tables=tables,
                cutoff=cutoff,
                keep_per_entity=keep_per_entity,
                batch_size=batch_size,
                max_batches=max_batches,
            )
            result["deleted"] = deleted
            result["has_more"] = has_more
            result["deleted_total"] = sum(int(value) for value in deleted.values())
            result["backlog_remaining"] = any(has_more.values())
            result["counts_after"] = _event_table_counts(connection, tables)
            result["database"] = _database_page_metrics(connection)
            _record_maintenance(
                connection,
                maintenance_key=_EVENT_MAINTENANCE_KEY,
                payload=result,
                has_more=result["backlog_remaining"],
            )
        return result

    def compact_metadata_result_history(
        self,
        *,
        max_result_bytes: int = _METADATA_RESULT_MAX_BYTES,
        batch_size: int = _METADATA_RESULT_COMPACT_BATCH_SIZE,
        max_batches: int = 20,
    ) -> dict[str, Any]:
        max_result_bytes = min(4 * 1024 * 1024, max(4_096, int(max_result_bytes)))
        batch_size = min(1_000, max(1, int(batch_size)))
        max_batches = min(100, max(1, int(max_batches)))
        result: dict[str, Any] = {
            "ok": True,
            "mode": "compact_metadata_result_history",
            "max_result_bytes": max_result_bytes,
            "batch_size": batch_size,
            "max_batches": max_batches,
        }

        with self._connect() as connection:
            result["before"] = _metadata_result_metrics(connection)
            compacted = _compact_metadata_result_rows(
                connection,
                max_result_bytes=max_result_bytes,
                batch_size=batch_size,
                max_batches=max_batches,
            )
            result.update(compacted)
            result["after"] = _metadata_result_metrics(connection)
            result["database"] = _database_page_metrics(connection)
            _record_maintenance(
                connection,
                maintenance_key=_METADATA_RESULT_MAINTENANCE_KEY,
                payload=result,
                has_more=bool(result["backlog_remaining"]),
            )
        return result

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = self._open_connection_with_retry()
        connection.row_factory = sqlite3.Row
        connection.execute("pragma busy_timeout = 30000")
        connection.execute("pragma synchronous = normal")
        connection.execute("pragma wal_autocheckpoint = 1000")
        connection.execute("pragma journal_size_limit = 67108864")
        try:
            yield connection
            connection.commit()
        except Exception:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def _open_connection_with_retry(self) -> sqlite3.Connection:
        last_error: sqlite3.Error | None = None
        for attempt in range(1, 7):
            try:
                return sqlite3.connect(self.path, timeout=30.0)
            except sqlite3.Error as exc:
                last_error = exc
                if not _is_transient_sqlite_error(exc) or attempt >= 6:
                    raise
                time.sleep(min(0.25 * attempt, 2.0))
        if last_error is not None:
            raise last_error
        raise sqlite3.OperationalError("sqlite connection failed without an exception")

    def _init_db(self) -> None:
        connection = self._open_connection_with_retry()
        connection.row_factory = sqlite3.Row
        connection.execute("pragma busy_timeout = 30000")
        try:
            connection.execute("pragma journal_mode = wal")
            connection.execute("pragma synchronous = normal")
            connection.execute("pragma wal_autocheckpoint = 1000")
            connection.execute("pragma journal_size_limit = 67108864")
            initialize_pipeline_state_schema(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _add_job_event(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        event: str,
        message: str,
    ) -> None:
        connection.execute(
            """
            insert into ocr_job_events (job_id, event, message, created_at)
            values (?, ?, ?, ?)
            """,
            (job_id, event, message, _utc_now().isoformat()),
        )
        _maybe_maintain_state_history(connection)

    @staticmethod
    def _add_html_job_event(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        event: str,
        message: str,
    ) -> None:
        connection.execute(
            """
            insert into html_job_events (job_id, event, message, created_at)
            values (?, ?, ?, ?)
            """,
            (job_id, event, message, _utc_now().isoformat()),
        )
        _maybe_maintain_state_history(connection)

    @staticmethod
    def _add_metadata_job_event(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        event: str,
        message: str,
    ) -> None:
        connection.execute(
            """
            insert into metadata_job_events (job_id, event, message, created_at)
            values (?, ?, ?, ?)
            """,
            (job_id, event, message, _utc_now().isoformat()),
        )
        _maybe_maintain_state_history(connection)

    @staticmethod
    def _add_full_run_event(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        event: str,
        message: str,
        metadata: dict[str, Any] | None,
    ) -> None:
        connection.execute(
            """
            insert into full_run_events (run_id, event, message, metadata, created_at)
            values (?, ?, ?, ?, ?)
            """,
            (run_id, event, message, _json_or_none(metadata), _utc_now().isoformat()),
        )
        _maybe_maintain_state_history(connection)


OcrStateStore = PipelineStateStore


def _existing_event_tables(connection: sqlite3.Connection) -> dict[str, str]:
    existing = {
        str(row[0])
        for row in connection.execute(
            "select name from sqlite_master where type = 'table'"
        ).fetchall()
    }
    return {
        table: entity_column
        for table, entity_column in _EVENT_HISTORY_TABLES.items()
        if table in existing
    }


def _event_table_counts(
    connection: sqlite3.Connection,
    tables: dict[str, str],
) -> dict[str, int]:
    return {
        table: int(connection.execute(f"select count(*) from {table}").fetchone()[0])
        for table in tables
    }


def _prune_event_tables(
    connection: sqlite3.Connection,
    *,
    tables: dict[str, str],
    cutoff: str,
    keep_per_entity: int,
    batch_size: int,
    max_batches: int,
) -> tuple[dict[str, int], dict[str, bool]]:
    deleted: dict[str, int] = {}
    has_more: dict[str, bool] = {}
    for table, entity_column in tables.items():
        table_deleted = 0
        for _batch in range(max_batches):
            deleted_now = _prune_event_table_batch(
                connection,
                table=table,
                entity_column=entity_column,
                cutoff=cutoff,
                keep_per_entity=keep_per_entity,
                batch_size=batch_size,
            )
            table_deleted += deleted_now
            if deleted_now < batch_size:
                break
        deleted[table] = table_deleted
        has_more[table] = _event_table_has_prunable_rows(
            connection,
            table=table,
            entity_column=entity_column,
            cutoff=cutoff,
            keep_per_entity=keep_per_entity,
        )
    return deleted, has_more


def _prune_event_table_batch(
    connection: sqlite3.Connection,
    *,
    table: str,
    entity_column: str,
    cutoff: str,
    keep_per_entity: int,
    batch_size: int,
) -> int:
    keep_clause, keep_params = _event_keep_clause(
        table=table,
        entity_column=entity_column,
        keep_per_entity=keep_per_entity,
    )
    cursor = connection.execute(
        f"""
        delete from {table}
        where event_id in (
          select old.event_id
          from {table} as old
          where old.created_at < ?
            {keep_clause}
          order by old.event_id
          limit ?
        )
        """,
        (cutoff, *keep_params, batch_size),
    )
    return max(0, int(cursor.rowcount or 0))


def _event_table_has_prunable_rows(
    connection: sqlite3.Connection,
    *,
    table: str,
    entity_column: str,
    cutoff: str,
    keep_per_entity: int,
) -> bool:
    keep_clause, keep_params = _event_keep_clause(
        table=table,
        entity_column=entity_column,
        keep_per_entity=keep_per_entity,
    )
    row = connection.execute(
        f"""
        select 1
        from {table} as old
        where old.created_at < ?
          {keep_clause}
        limit 1
        """,
        (cutoff, *keep_params),
    ).fetchone()
    return row is not None


def _event_keep_clause(
    *,
    table: str,
    entity_column: str,
    keep_per_entity: int,
) -> tuple[str, tuple[int, ...]]:
    if keep_per_entity <= 0:
        return "", ()
    return (
        f"""
        and exists (
          select 1
          from {table} as newer
          where newer.{entity_column} = old.{entity_column}
            and newer.event_id > old.event_id
          order by newer.event_id
          limit 1 offset ?
        )
        """,
        (keep_per_entity - 1,),
    )


def _metadata_result_metrics(connection: sqlite3.Connection) -> dict[str, int]:
    row = connection.execute(
        """
        select count(*) as rows,
               coalesce(sum(length(cast(result_json as blob))), 0) as bytes,
               coalesce(max(length(cast(result_json as blob))), 0) as max_bytes,
               coalesce(sum(case when result_json like '{"_compacted":%' then 1 else 0 end), 0)
                 as compacted_rows
        from metadata_jobs
        where result_json is not null
        """
    ).fetchone()
    return {
        "rows": int(row["rows"] or 0),
        "bytes": int(row["bytes"] or 0),
        "max_bytes": int(row["max_bytes"] or 0),
        "compacted_rows": int(row["compacted_rows"] or 0),
    }


def _compact_metadata_result_rows(
    connection: sqlite3.Connection,
    *,
    max_result_bytes: int,
    batch_size: int,
    max_batches: int,
) -> dict[str, Any]:
    compacted_rows = 0
    original_bytes = 0
    stored_bytes = 0
    blocked_rows = 0
    for _batch in range(max_batches):
        rows = connection.execute(
            """
            select job_id, result_json
            from metadata_jobs
            where status in ('succeeded', 'skipped', 'failed_final')
              and result_json is not null
              and result_json not like '{"_compacted":%'
              and length(cast(result_json as blob)) > ?
            order by updated_at, job_id
            limit ?
            """,
            (max_result_bytes, batch_size),
        ).fetchall()
        if not rows:
            break
        changed_this_batch = 0
        for row in rows:
            raw = str(row["result_json"])
            compacted = _compact_metadata_result_json(
                raw,
                max_result_bytes=max_result_bytes,
            )
            raw_bytes = _json_bytes(raw)
            if compacted is None:
                blocked_rows += 1
                continue
            compacted_bytes = _json_bytes(compacted)
            if compacted == raw or compacted_bytes >= raw_bytes:
                blocked_rows += 1
                continue
            cursor = connection.execute(
                """
                update metadata_jobs
                set result_json = ?
                where job_id = ? and result_json = ?
                """,
                (compacted, str(row["job_id"]), raw),
            )
            if cursor.rowcount != 1:
                continue
            compacted_rows += 1
            changed_this_batch += 1
            original_bytes += raw_bytes
            stored_bytes += compacted_bytes
        if len(rows) < batch_size or changed_this_batch == 0:
            break

    backlog_remaining = _metadata_result_has_compaction_candidates(
        connection,
        max_result_bytes=max_result_bytes,
    )
    return {
        "compacted_rows": compacted_rows,
        "blocked_rows": blocked_rows,
        "original_bytes": original_bytes,
        "stored_bytes": stored_bytes,
        "logical_bytes_reclaimed": max(0, original_bytes - stored_bytes),
        "backlog_remaining": backlog_remaining,
    }


def _metadata_result_has_compaction_candidates(
    connection: sqlite3.Connection,
    *,
    max_result_bytes: int,
) -> bool:
    row = connection.execute(
        """
        select 1
        from metadata_jobs
        where status in ('succeeded', 'skipped', 'failed_final')
          and result_json is not null
          and result_json not like '{"_compacted":%'
          and length(cast(result_json as blob)) > ?
        limit 1
        """,
        (max_result_bytes,),
    ).fetchone()
    return row is not None


def _compact_metadata_result_json(
    raw: str | None,
    *,
    max_result_bytes: int = _METADATA_RESULT_MAX_BYTES,
) -> str | None:
    if raw is None or _json_bytes(raw) <= max_result_bytes:
        return raw
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("_compacted"), dict):
        return raw

    refs, refs_overflow = _collect_downstream_refs(payload)
    if refs_overflow:
        return raw
    top_level_keys = (
        [str(key)[:256] for key in islice(payload, 128)]
        if isinstance(payload, dict)
        else []
    )
    compact: dict[str, Any] = {
        "_compacted": {
            "schema": 1,
            "reason": "terminal_metadata_result_limit",
            "original_bytes": _json_bytes(raw),
            "original_sha256": sha256(raw.encode("utf-8")).hexdigest(),
            "original_type": type(payload).__name__ if payload is not None else "invalid_json",
            "top_level_keys": top_level_keys,
            "omitted_top_level_key_count": (
                max(0, len(payload) - len(top_level_keys))
                if isinstance(payload, dict)
                else 0
            ),
        },
        "downstream_refs": refs,
    }
    summary: dict[str, Any] = {}
    omitted: list[str] = []
    items = (
        islice(payload.items(), 128)
        if isinstance(payload, dict)
        else iter((("value", payload),))
    )
    for key, value in items:
        normalized_key = str(key)[:256]
        candidate = _compact_preview(value)
        trial = {**compact, "summary": {**summary, normalized_key: candidate}}
        if _json_bytes(_json_or_none(trial) or "") <= max_result_bytes:
            summary[normalized_key] = candidate
        else:
            omitted.append(normalized_key)
    compact["summary"] = summary
    if omitted:
        compact["_compacted"]["omitted_top_level_keys"] = omitted[:128]

    serialized = _json_or_none(compact) or ""
    if _json_bytes(serialized) <= max_result_bytes:
        return serialized
    compact["_compacted"].pop("omitted_top_level_keys", None)
    compact["_compacted"].pop("top_level_keys", None)
    compact.pop("summary", None)
    serialized = _json_or_none(compact) or ""
    return serialized if _json_bytes(serialized) < _json_bytes(raw) else raw


def _collect_downstream_refs(value: Any) -> tuple[list[dict[str, Any]], bool]:
    refs: list[dict[str, Any]] = []
    stack = [value]
    scanned = 0
    while stack:
        current = stack.pop()
        scanned += 1
        if scanned > _MAX_COMPACT_SCAN_NODES:
            return refs, True
        if isinstance(current, dict):
            if current.get("classification") == "downstream_orchestrator":
                normalized = _normalize_downstream_ref(current)
                if len(refs) >= _MAX_DOWNSTREAM_REFS:
                    return refs, True
                refs.append(normalized)
                continue
            stack.extend(reversed(current.values()))
        elif isinstance(current, list):
            stack.extend(reversed(current))
    return refs, False


def _normalize_downstream_ref(value: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: value[key]
        for key in (
            "ok",
            "skipped",
            "delegated",
            "classification",
            "stage",
            "reason",
            "source_path",
        )
        if key in value
    }
    attachment = value.get("attachment")
    if isinstance(attachment, dict):
        result["attachment"] = {
            key: attachment[key]
            for key in (
                "library_id",
                "data_dir",
                "storage_dir",
                "key",
                "item_id",
                "parent_item_id",
                "date_modified",
                "link_mode",
                "content_type",
                "zotero_path",
                "file_path",
                "parent_key",
            )
            if key in attachment
        }
    return result


def _compact_preview(value: Any, *, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= _COMPACT_PREVIEW_MAX_STRING_CHARS:
            return value
        return {
            "_type": "string",
            "chars": len(value),
            "preview": value[:_COMPACT_PREVIEW_MAX_STRING_CHARS],
        }
    if depth >= _COMPACT_PREVIEW_MAX_DEPTH:
        size = len(value) if isinstance(value, (dict, list)) else None
        return {"_type": type(value).__name__, "items": size}
    if isinstance(value, dict):
        if value.get("classification") == "downstream_orchestrator":
            return {
                "_type": "downstream_orchestrator_ref",
                "stage": value.get("stage"),
            }
        preview = {
            str(key)[:256]: _compact_preview(child, depth=depth + 1)
            for key, child in islice(value.items(), _COMPACT_PREVIEW_MAX_ITEMS)
        }
        if len(value) > _COMPACT_PREVIEW_MAX_ITEMS:
            preview["_omitted_items"] = len(value) - _COMPACT_PREVIEW_MAX_ITEMS
        return preview
    if isinstance(value, list):
        preview_list = [
            _compact_preview(child, depth=depth + 1)
            for child in value[:_COMPACT_PREVIEW_MAX_ITEMS]
        ]
        if len(value) > _COMPACT_PREVIEW_MAX_ITEMS:
            preview_list.append(
                {"_omitted_items": len(value) - _COMPACT_PREVIEW_MAX_ITEMS}
            )
        return preview_list
    return str(value)[:_COMPACT_PREVIEW_MAX_STRING_CHARS]


def _json_bytes(value: str) -> int:
    return len(value.encode("utf-8"))


def _database_page_metrics(connection: sqlite3.Connection) -> dict[str, int]:
    page_count = int(connection.execute("pragma page_count").fetchone()[0])
    page_size = int(connection.execute("pragma page_size").fetchone()[0])
    freelist_count = int(connection.execute("pragma freelist_count").fetchone()[0])
    return {
        "page_count": page_count,
        "page_size": page_size,
        "freelist_count": freelist_count,
        "allocated_bytes": page_count * page_size,
        "reusable_bytes": freelist_count * page_size,
    }


def _maybe_maintain_state_history(connection: sqlite3.Connection) -> None:
    try:
        now = _utc_now()
        if _maintenance_due(connection, _EVENT_MAINTENANCE_KEY, now=now):
            tables = _existing_event_tables(connection)
            deleted, has_more = _prune_event_tables(
                connection,
                tables=tables,
                cutoff=(now - timedelta(days=_EVENT_RETENTION_DAYS)).isoformat(),
                keep_per_entity=_EVENT_KEEP_PER_ENTITY,
                batch_size=_EVENT_PRUNE_BATCH_SIZE,
                max_batches=1,
            )
            _record_maintenance(
                connection,
                maintenance_key=_EVENT_MAINTENANCE_KEY,
                payload={"deleted": deleted, "has_more": has_more},
                has_more=any(has_more.values()),
                now=now,
            )
        if _maintenance_due(connection, _METADATA_RESULT_MAINTENANCE_KEY, now=now):
            compacted = _compact_metadata_result_rows(
                connection,
                max_result_bytes=_METADATA_RESULT_MAX_BYTES,
                batch_size=_METADATA_RESULT_COMPACT_BATCH_SIZE,
                max_batches=1,
            )
            _record_maintenance(
                connection,
                maintenance_key=_METADATA_RESULT_MAINTENANCE_KEY,
                payload=compacted,
                has_more=bool(compacted["backlog_remaining"]),
                now=now,
            )
    except (sqlite3.Error, TypeError, ValueError, UnicodeError, RecursionError):
        return


def _maintenance_due(
    connection: sqlite3.Connection,
    maintenance_key: str,
    *,
    now: datetime,
) -> bool:
    row = connection.execute(
        "select next_run_at from state_maintenance where maintenance_key = ?",
        (maintenance_key,),
    ).fetchone()
    return row is None or str(row["next_run_at"] or "") <= now.isoformat()


def _record_maintenance(
    connection: sqlite3.Connection,
    *,
    maintenance_key: str,
    payload: dict[str, Any],
    has_more: bool,
    now: datetime | None = None,
) -> None:
    current = now or _utc_now()
    interval = (
        _MAINTENANCE_BACKLOG_INTERVAL_SECONDS
        if has_more
        else _MAINTENANCE_INTERVAL_SECONDS
    )
    next_run_at = (current + timedelta(seconds=interval)).isoformat()
    connection.execute(
        """
        insert into state_maintenance (maintenance_key, next_run_at, payload, updated_at)
        values (?, ?, ?, ?)
        on conflict(maintenance_key) do update set
          next_run_at = excluded.next_run_at,
          payload = excluded.payload,
          updated_at = excluded.updated_at
        """,
        (
            maintenance_key,
            next_run_at,
            _json_or_none(payload),
            current.isoformat(),
        ),
    )


def _is_transient_sqlite_error(exc: sqlite3.Error) -> bool:
    message = str(exc).lower()
    return any(
        fragment in message
        for fragment in (
            "database is locked",
            "database is busy",
            "disk i/o error",
            "unable to open database file",
        )
    )


def _terminal_owner_clause(owner: str | None) -> tuple[str, tuple[str, ...]]:
    normalized = str(owner or "").strip()
    if not normalized:
        return "", ()
    return (
        "and (lease_owner = ? or (status = 'queued' and lease_owner is null))",
        (normalized,),
    )


def _stale_job_update_message(queue_name: str, owner: str | None) -> str:
    normalized = str(owner or "").strip()
    if not normalized:
        return f"Stale {queue_name} job update was discarded because the job state changed."
    return (
        f"Stale {queue_name} job update was discarded because the job is no longer "
        f"leased by {normalized}."
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _lease_owner_process_alive(owner: str) -> bool:
    pid = _lease_owner_pid(owner)
    if pid is None:
        return True
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lease_owner_pid(owner: str) -> int | None:
    tail = str(owner or "").rsplit(":", 1)[-1].strip()
    if not tail.isdigit():
        return None
    pid = int(tail)
    return pid if pid > 0 else None


def _windows_pid_alive(pid: int) -> bool:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    process_query_limited_information = 0x1000
    error_access_denied = 5
    still_active = 259
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ctypes.get_last_error() == error_access_denied
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return int(exit_code.value) == still_active
    finally:
        kernel32.CloseHandle(handle)


def _json_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value) if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _html_relay_status(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        attachments = value.get("attachments")
        if isinstance(attachments, dict):
            if not attachments:
                return "skipped"
            failed_optional = False
            for result in attachments.values():
                if not isinstance(result, dict):
                    continue
                if result.get("ok") is False:
                    failed_optional = True
                    continue
                relay = result.get("relay")
                if isinstance(relay, dict) and relay.get("ok") is False:
                    failed_optional = True
            return "failed_optional" if failed_optional else "succeeded"
        if value.get("ok") is False:
            return "failed_optional"
    return "succeeded"


def _metadata_relay_status(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        if value.get("ok") is False:
            return "failed"
        if value.get("dryRun"):
            return "dry_run"
        if value.get("skipped") is True:
            return "skipped"
    return "succeeded"


def _metadata_failure_is_transient(last_error: str) -> bool:
    message = str(last_error or "").casefold()
    if not message:
        return False
    transient_markers = (
        "http 408",
        "http 409",
        "http 425",
        "http 429",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
        "timed out",
        "timeout",
        "getaddrinfo failed",
        "connection refused",
        "connection reset",
        "winerror 10054",
        "winerror 10060",
        "удаленный хост принудительно разорвал",
        "попытка установить соединение",
        "temporary failure",
        "temporarily unavailable",
        "too many requests",
    )
    return any(marker in message for marker in transient_markers)


def _job_dedupe_key(
    *,
    library_id: str,
    attachment_key: str,
    signature: FileSignature,
    force: bool,
) -> str:
    force_flag = "1" if force else "0"
    return f"{library_id}:{attachment_key}:{signature.size}:{signature.mtime_ns}:{force_flag}"


def _html_job_dedupe_key(
    *,
    library_id: str,
    attachment_key: str,
    signature: FileSignature,
    force: bool,
    pipeline_key: str,
) -> str:
    force_flag = "1" if force else "0"
    return (
        f"html:{library_id}:{attachment_key}:{signature.size}:"
        f"{signature.mtime_ns}:{force_flag}:{pipeline_key}"
    )


def _metadata_job_dedupe_key(
    *,
    job_type: str,
    library_id: str,
    attachment_key: str,
    signature: FileSignature,
    force: bool,
    queue_key: str,
) -> str:
    force_flag = "1" if force else "0"
    return (
        f"metadata:{job_type}:{library_id}:{attachment_key}:"
        f"{signature.size}:{signature.mtime_ns}:{force_flag}:{queue_key}"
    )


def _safe_job_type(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in str(value or "job")).strip("_") or "job"
