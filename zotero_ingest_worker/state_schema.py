from __future__ import annotations

import sqlite3


PIPELINE_STATE_SCHEMA_VERSION = 3


def initialize_pipeline_state_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        create table if not exists ocr_state (
          attachment_key text primary key,
          source_path text not null,
          source_size integer not null,
          source_mtime_ns integer not null,
          status text not null,
          message text not null,
          text_chars integer not null default 0,
          result_path text,
          backup_path text,
          updated_at text not null
        )
        """
    )
    connection.execute(
        """
        create table if not exists watch_state (
          source_path text primary key,
          source_size integer not null,
          source_mtime_ns integer not null,
          status text not null,
          message text not null,
          attachment_key text,
          stable_seen_count integer not null default 0,
          first_seen_at text,
          last_seen_at text,
          last_error text,
          updated_at text not null
        )
        """
    )
    _add_column_if_missing(
        connection,
        "watch_state",
        "stable_seen_count",
        "integer not null default 0",
    )
    _add_column_if_missing(connection, "watch_state", "first_seen_at", "text")
    _add_column_if_missing(connection, "watch_state", "last_seen_at", "text")
    _add_column_if_missing(connection, "watch_state", "last_error", "text")
    connection.execute(
        """
        create table if not exists ocr_jobs (
          job_id text primary key,
          dedupe_key text not null unique,
          library_id text not null,
          attachment_key text not null,
          data_dir text not null,
          source_path text not null,
          source_size integer not null,
          source_mtime_ns integer not null,
          status text not null,
          reason text not null,
          force integer not null default 0,
          attempts integer not null default 0,
          max_attempts integer not null default 3,
          phase text,
          lease_owner text,
          leased_until text,
          last_error text,
          result_path text,
          backup_path text,
          relay_status text,
          relay_result text,
          created_at text not null,
          updated_at text not null
        )
        """
    )
    connection.execute(
        """
        create table if not exists ocr_job_events (
          event_id integer primary key autoincrement,
          job_id text not null,
          event text not null,
          message text,
          created_at text not null
        )
        """
    )
    connection.execute(
        """
        create table if not exists html_jobs (
          job_id text primary key,
          dedupe_key text not null unique,
          library_id text not null,
          attachment_key text not null,
          data_dir text not null,
          source_path text not null,
          source_size integer not null,
          source_mtime_ns integer not null,
          collection_key text not null,
          pipeline_key text not null,
          status text not null,
          reason text not null,
          force integer not null default 0,
          attempts integer not null default 0,
          max_attempts integer not null default 3,
          phase text,
          lease_owner text,
          leased_until text,
          last_error text,
          en_html_path text,
          ru_html_path text,
          source_language text,
          target_language text,
          translation_skipped_reason text,
          relay_status text,
          relay_result text,
          created_at text not null,
          updated_at text not null
        )
        """
    )
    _add_column_if_missing(connection, "html_jobs", "source_language", "text")
    _add_column_if_missing(connection, "html_jobs", "target_language", "text")
    _add_column_if_missing(
        connection,
        "html_jobs",
        "translation_skipped_reason",
        "text",
    )
    connection.execute(
        """
        create table if not exists html_job_events (
          event_id integer primary key autoincrement,
          job_id text not null,
          event text not null,
          message text,
          created_at text not null
        )
        """
    )
    connection.execute(
        """
        create table if not exists metadata_jobs (
          job_id text primary key,
          job_type text not null,
          dedupe_key text not null unique,
          library_id text not null,
          attachment_key text not null,
          data_dir text not null,
          source_path text not null,
          source_size integer not null,
          source_mtime_ns integer not null,
          parent_item_key text,
          parent_version integer,
          queue_key text not null default 'default',
          status text not null,
          reason text not null,
          force integer not null default 0,
          attempts integer not null default 0,
          max_attempts integer not null default 3,
          phase text,
          lease_owner text,
          leased_until text,
          last_error text,
          result_json text,
          output_path text,
          relay_status text,
          relay_result text,
          created_at text not null,
          updated_at text not null
        )
        """
    )
    connection.execute(
        """
        create table if not exists metadata_job_events (
          event_id integer primary key autoincrement,
          job_id text not null,
          event text not null,
          message text,
          created_at text not null
        )
        """
    )
    connection.execute(
        """
        create table if not exists problem_documents (
          source_path text primary key,
          library_id text not null,
          attachment_key text not null,
          data_dir text not null,
          source_size integer not null,
          source_mtime_ns integer not null,
          problem_status text not null,
          reason text not null,
          first_seen_at text not null,
          last_seen_at text not null,
          resolved_at text,
          metadata text
        )
        """
    )
    connection.execute("create index if not exists idx_ocr_jobs_status on ocr_jobs(status)")
    connection.execute(
        "create index if not exists idx_ocr_jobs_attachment on ocr_jobs(library_id, attachment_key)"
    )
    connection.execute("create index if not exists idx_html_jobs_status on html_jobs(status)")
    connection.execute(
        "create index if not exists idx_html_jobs_attachment on html_jobs(library_id, attachment_key)"
    )
    connection.execute(
        "create index if not exists idx_metadata_jobs_type_status on metadata_jobs(job_type, status)"
    )
    connection.execute(
        """
        create index if not exists idx_metadata_jobs_type_library_status
        on metadata_jobs(job_type, library_id, status)
        """
    )
    connection.execute(
        "create index if not exists idx_metadata_jobs_attachment on metadata_jobs(library_id, attachment_key)"
    )
    connection.execute(
        "create index if not exists idx_problem_documents_status on problem_documents(problem_status)"
    )
    connection.execute(
        """
        create table if not exists full_runs (
          run_id text primary key,
          status text not null,
          phase text,
          order_mode text not null,
          options text,
          current_job_kind text,
          current_job_id text,
          stop_requested integer not null default 0,
          last_error text,
          started_at text not null,
          updated_at text not null,
          heartbeat_at text,
          finished_at text
        )
        """
    )
    connection.execute(
        """
        create table if not exists full_run_events (
          event_id integer primary key autoincrement,
          run_id text not null,
          event text not null,
          message text,
          metadata text,
          created_at text not null
        )
        """
    )
    connection.execute(
        """
        create table if not exists state_maintenance (
          maintenance_key text primary key,
          next_run_at text not null,
          payload text,
          updated_at text not null
        )
        """
    )
    connection.execute("create index if not exists idx_full_runs_status on full_runs(status)")
    connection.execute(
        "create index if not exists idx_ocr_job_events_entity on ocr_job_events(job_id, event_id)"
    )
    connection.execute(
        "create index if not exists idx_ocr_job_events_created on ocr_job_events(created_at, event_id)"
    )
    connection.execute(
        "create index if not exists idx_html_job_events_entity on html_job_events(job_id, event_id)"
    )
    connection.execute(
        "create index if not exists idx_html_job_events_created on html_job_events(created_at, event_id)"
    )
    connection.execute(
        "create index if not exists idx_metadata_job_events_entity "
        "on metadata_job_events(job_id, event_id)"
    )
    connection.execute(
        "create index if not exists idx_metadata_job_events_created "
        "on metadata_job_events(created_at, event_id)"
    )
    connection.execute(
        "create index if not exists idx_full_run_events_entity on full_run_events(run_id, event_id)"
    )
    connection.execute(
        "create index if not exists idx_full_run_events_created on full_run_events(created_at, event_id)"
    )

    current_version = int(connection.execute("pragma user_version").fetchone()[0])
    if current_version < PIPELINE_STATE_SCHEMA_VERSION:
        connection.execute(f"pragma user_version = {PIPELINE_STATE_SCHEMA_VERSION}")


def _add_column_if_missing(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    rows = connection.execute(f"pragma table_info({table})").fetchall()
    existing = {str(row["name"]) for row in rows}
    if column in existing:
        return
    connection.execute(f"alter table {table} add column {column} {definition}")
