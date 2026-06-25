from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .state_schema import PIPELINE_STATE_SCHEMA_VERSION, initialize_pipeline_state_schema

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
        return row["source_size"] == signature.size and row["source_mtime_ns"] == signature.mtime_ns

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
        return row["source_size"] == signature.size and row["source_mtime_ns"] == signature.mtime_ns

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
            for job_id in job_ids:
                connection.execute(
                    """
                    update html_jobs
                    set status = 'queued',
                        phase = 'recovered',
                        lease_owner = null,
                        leased_until = null,
                        last_error = 'Recovered expired running HTML job lease.',
                        updated_at = ?
                    where job_id = ?
                    """,
                    (now, job_id),
                )
                self._add_html_job_event(
                    connection,
                    job_id=job_id,
                    event="recovered",
                    message="Expired running HTML job lease was returned to queued.",
                )
        return len(job_ids)

    def lease_next_html_job(self, *, owner: str, lease_seconds: int) -> dict[str, Any] | None:
        self.recover_expired_html_jobs()
        now = _utc_now()
        leased_until = now + timedelta(seconds=lease_seconds)
        with self._connect() as connection:
            row = connection.execute(
                """
                select *
                from html_jobs
                where status = 'queued'
                order by created_at asc
                limit 1
                """
            ).fetchone()
            if row is None:
                return None
            job = dict(row)
            connection.execute(
                """
                update html_jobs
                set status = 'running',
                    phase = 'leased',
                    attempts = attempts + 1,
                    lease_owner = ?,
                    leased_until = ?,
                    updated_at = ?
                where job_id = ?
                """,
                (owner, leased_until.isoformat(), now.isoformat(), job["job_id"]),
            )
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
    ) -> dict[str, Any]:
        now = _utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
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
                ),
            )
            self._add_html_job_event(connection, job_id=job_id, event="succeeded", message=message)
        return self.get_html_job(job_id) or {}

    def mark_html_job_deferred(
        self,
        *,
        job_id: str,
        status: str,
        message: str,
        metadata: Any = None,
    ) -> dict[str, Any]:
        now = _utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
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
                """,
                (status, message, _json_or_none(metadata), now, job_id),
            )
            self._add_html_job_event(connection, job_id=job_id, event=status, message=message)
        return self.get_html_job(job_id) or {}

    def mark_html_job_failed(
        self,
        *,
        job_id: str,
        message: str,
        retryable: bool,
    ) -> dict[str, Any]:
        job = self.get_html_job(job_id)
        if job is None:
            return {}
        attempts = int(job["attempts"])
        max_attempts = int(job["max_attempts"])
        status = "failed_retryable" if retryable and attempts < max_attempts else "failed_final"
        now = _utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                update html_jobs
                set status = ?,
                    phase = 'failed',
                    lease_owner = null,
                    leased_until = null,
                    last_error = ?,
                    updated_at = ?
                where job_id = ?
                """,
                (status, message, now, job_id),
            )
            self._add_html_job_event(connection, job_id=job_id, event=status, message=message)
        return self.get_html_job(job_id) or {}

    def retry_html_job(self, job_id: str) -> dict[str, Any]:
        now = _utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                update html_jobs
                set status = 'queued',
                    phase = 'queued',
                    lease_owner = null,
                    leased_until = null,
                    updated_at = ?
                where job_id = ?
                  and status in ('failed_retryable', 'failed_final', 'cancelled', 'needs_chunk_fallback')
                """,
                (now, job_id),
            )
            self._add_html_job_event(
                connection,
                job_id=job_id,
                event="retry",
                message="HTML job returned to queue.",
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

    def metadata_queue_summary(self, *, job_type: str | None = None) -> dict[str, Any]:
        params: list[Any] = []
        where = ""
        if job_type:
            where = "where job_type = ?"
            params.append(job_type)
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
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return {
            "job_type": job_type,
            "queued": counts.get("queued", 0),
            "running": counts.get("running", 0),
            "succeeded": counts.get("succeeded", 0),
            "skipped": counts.get("skipped", 0),
            "failed_retryable": counts.get("failed_retryable", 0),
            "failed_final": counts.get("failed_final", 0),
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
            for job_id in job_ids:
                connection.execute(
                    """
                    update metadata_jobs
                    set status = 'queued',
                        phase = 'recovered',
                        lease_owner = null,
                        leased_until = null,
                        last_error = 'Recovered expired metadata job lease.',
                        updated_at = ?
                    where job_id = ?
                    """,
                    (now, job_id),
                )
                self._add_metadata_job_event(
                    connection,
                    job_id=job_id,
                    event="recovered",
                    message="Expired running metadata job lease was returned to queued.",
                )
        return len(job_ids)

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
            row = connection.execute(
                """
                select *
                from metadata_jobs
                where job_type = ?
                  and status = 'queued'
                order by created_at asc
                limit 1
                """,
                (job_type,),
            ).fetchone()
            if row is None:
                return None
            job = dict(row)
            connection.execute(
                """
                update metadata_jobs
                set status = 'running',
                    phase = 'leased',
                    attempts = attempts + 1,
                    lease_owner = ?,
                    leased_until = ?,
                    updated_at = ?
                where job_id = ?
                """,
                (owner, leased_until.isoformat(), now.isoformat(), job["job_id"]),
            )
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
    ) -> dict[str, Any]:
        now = _utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
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
                """,
                (
                    _json_or_none(result),
                    output_path,
                    _metadata_relay_status(relay_result),
                    _json_or_none(relay_result),
                    now,
                    job_id,
                ),
            )
            self._add_metadata_job_event(connection, job_id=job_id, event="succeeded", message=message)
        return self.get_metadata_job(job_id) or {}

    def mark_metadata_job_skipped(
        self,
        *,
        job_id: str,
        message: str,
        result: Any = None,
    ) -> dict[str, Any]:
        now = _utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
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
                """,
                (message, _json_or_none(result), now, job_id),
            )
            self._add_metadata_job_event(connection, job_id=job_id, event="skipped", message=message)
        return self.get_metadata_job(job_id) or {}

    def mark_metadata_job_failed(
        self,
        *,
        job_id: str,
        message: str,
        retryable: bool,
    ) -> dict[str, Any]:
        job = self.get_metadata_job(job_id)
        if job is None:
            return {}
        attempts = int(job["attempts"])
        max_attempts = int(job["max_attempts"])
        status = "failed_retryable" if retryable and attempts < max_attempts else "failed_final"
        now = _utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                update metadata_jobs
                set status = ?,
                    phase = 'failed',
                    lease_owner = null,
                    leased_until = null,
                    last_error = ?,
                    updated_at = ?
                where job_id = ?
                """,
                (status, message, now, job_id),
            )
            self._add_metadata_job_event(connection, job_id=job_id, event=status, message=message)
        return self.get_metadata_job(job_id) or {}

    def retry_metadata_job(self, job_id: str) -> dict[str, Any]:
        now = _utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                update metadata_jobs
                set status = 'queued',
                    phase = 'queued',
                    lease_owner = null,
                    leased_until = null,
                    updated_at = ?
                where job_id = ?
                  and status in ('failed_retryable', 'failed_final', 'skipped', 'cancelled')
                """,
                (now, job_id),
            )
            self._add_metadata_job_event(
                connection,
                job_id=job_id,
                event="retry",
                message="Metadata job returned to queue.",
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
            for job_id in job_ids:
                connection.execute(
                    """
                    update ocr_jobs
                    set status = 'queued',
                        phase = 'recovered',
                        lease_owner = null,
                        leased_until = null,
                        last_error = 'Recovered expired running job lease.',
                        updated_at = ?
                    where job_id = ?
                    """,
                    (now, job_id),
                )
                self._add_job_event(
                    connection,
                    job_id=job_id,
                    event="recovered",
                    message="Expired running job lease was returned to queued.",
                )
        return len(job_ids)

    def lease_next_job(self, *, owner: str, lease_seconds: int) -> dict[str, Any] | None:
        self.recover_expired_jobs()
        now = _utc_now()
        leased_until = now + timedelta(seconds=lease_seconds)
        with self._connect() as connection:
            row = connection.execute(
                """
                select *
                from ocr_jobs
                where status = 'queued'
                order by created_at asc
                limit 1
                """
            ).fetchone()
            if row is None:
                return None
            job = dict(row)
            connection.execute(
                """
                update ocr_jobs
                set status = 'running',
                    phase = 'leased',
                    attempts = attempts + 1,
                    lease_owner = ?,
                    leased_until = ?,
                    updated_at = ?
                where job_id = ?
                """,
                (owner, leased_until.isoformat(), now.isoformat(), job["job_id"]),
            )
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
    ) -> dict[str, Any]:
        now = _utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
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
                """,
                (
                    result_path,
                    backup_path,
                    "succeeded" if relay_result is not None else None,
                    _json_or_none(relay_result),
                    now,
                    job_id,
                ),
            )
            self._add_job_event(connection, job_id=job_id, event="succeeded", message=message)
        return self.get_job(job_id) or {}

    def mark_job_failed(
        self,
        *,
        job_id: str,
        message: str,
        retryable: bool,
    ) -> dict[str, Any]:
        job = self.get_job(job_id)
        if job is None:
            return {}
        attempts = int(job["attempts"])
        max_attempts = int(job["max_attempts"])
        status = "failed_retryable" if retryable and attempts < max_attempts else "failed_final"
        now = _utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                update ocr_jobs
                set status = ?,
                    phase = 'failed',
                    lease_owner = null,
                    leased_until = null,
                    last_error = ?,
                    updated_at = ?
                where job_id = ?
                """,
                (status, message, now, job_id),
            )
            self._add_job_event(connection, job_id=job_id, event=status, message=message)
        return self.get_job(job_id) or {}

    def mark_job_manual_review(self, *, job_id: str, message: str) -> dict[str, Any]:
        now = _utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                update ocr_jobs
                set status = 'manual_review',
                    phase = 'manual_review',
                    lease_owner = null,
                    leased_until = null,
                    last_error = ?,
                    updated_at = ?
                where job_id = ?
                """,
                (message, now, job_id),
            )
            self._add_job_event(connection, job_id=job_id, event="manual_review", message=message)
        return self.get_job(job_id) or {}

    def mark_job_problem_document(self, *, job_id: str, message: str) -> dict[str, Any]:
        now = _utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                update ocr_jobs
                set status = 'problem_document',
                    phase = 'problem_document',
                    lease_owner = null,
                    leased_until = null,
                    last_error = ?,
                    updated_at = ?
                where job_id = ?
                """,
                (message, now, job_id),
            )
            self._add_job_event(connection, job_id=job_id, event="problem_document", message=message)
        return self.get_job(job_id) or {}

    def retry_job(self, job_id: str) -> dict[str, Any]:
        now = _utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                update ocr_jobs
                set status = 'queued',
                    phase = 'queued',
                    lease_owner = null,
                    leased_until = null,
                    updated_at = ?
                where job_id = ?
                  and status in ('failed_retryable', 'failed_final', 'manual_review', 'problem_document', 'cancelled')
                """,
                (now, job_id),
            )
            self._add_job_event(connection, job_id=job_id, event="retry", message="Job returned to queue.")
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

    def mark_job_progress(self, *, job_id: str, phase: str, message: str) -> dict[str, Any]:
        now = _utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                update ocr_jobs
                set phase = ?,
                    updated_at = ?
                where job_id = ?
                """,
                (phase, now, job_id),
            )
            self._add_job_event(connection, job_id=job_id, event="progress", message=message)
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

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("pragma busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _init_db(self) -> None:
        with self._connect() as connection:
            initialize_pipeline_state_schema(connection)

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


OcrStateStore = PipelineStateStore


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


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
