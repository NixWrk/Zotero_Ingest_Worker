import json
import os
from concurrent.futures import ThreadPoolExecutor

from zotero_ingest_worker.state import (
    FileSignature,
    PIPELINE_STATE_SCHEMA_VERSION,
    OcrStateStore,
    PipelineStateStore,
)


def test_pipeline_state_store_sets_schema_version(tmp_path):
    store = PipelineStateStore(tmp_path / "state.sqlite")

    assert store.schema_version() == PIPELINE_STATE_SCHEMA_VERSION


def test_pipeline_state_store_enables_wal_and_maintenance_indexes(tmp_path):
    store = PipelineStateStore(tmp_path / "state.sqlite")

    with store._connect() as connection:
        journal_mode = str(connection.execute("pragma journal_mode").fetchone()[0])
        busy_timeout = int(connection.execute("pragma busy_timeout").fetchone()[0])
        indexes = {
            str(row["name"])
            for row in connection.execute(
                "select name from sqlite_master where type = 'index'"
            ).fetchall()
        }

    assert journal_mode == "wal"
    assert busy_timeout == 30_000
    assert {
        "idx_ocr_job_events_entity",
        "idx_ocr_job_events_created",
        "idx_html_job_events_entity",
        "idx_html_job_events_created",
        "idx_metadata_job_events_entity",
        "idx_metadata_job_events_created",
        "idx_full_run_events_entity",
        "idx_full_run_events_created",
    } <= indexes


def test_pipeline_state_v2_upgrade_preserves_jobs_and_adds_maintenance(tmp_path):
    path = tmp_path / "state.sqlite"
    source = tmp_path / "zotero.sqlite"
    source.write_bytes(b"state")
    store = PipelineStateStore(path)
    created = store.enqueue_metadata_job(
        job_type="full_text",
        library_id="LIB1",
        attachment_key="PARENT-UPGRADE",
        data_dir=tmp_path,
        source_path=source,
        signature=FileSignature.from_path(source),
        status="queued",
        reason="test",
        parent_item_key="PARENT-UPGRADE",
        queue_key="full-text-v1",
    )
    with store._connect() as connection:
        connection.execute("drop table state_maintenance")
        connection.execute("drop index idx_metadata_job_events_entity")
        connection.execute("pragma user_version = 2")

    upgraded = PipelineStateStore(path)

    assert upgraded.schema_version() == PIPELINE_STATE_SCHEMA_VERSION
    assert upgraded.get_metadata_job(str(created["job_id"])) is not None
    with upgraded._connect() as connection:
        maintenance_table = connection.execute(
            "select 1 from sqlite_master where type = 'table' and name = 'state_maintenance'"
        ).fetchone()
        event_index = connection.execute(
            "select 1 from sqlite_master where type = 'index' "
            "and name = 'idx_metadata_job_events_entity'"
        ).fetchone()
    assert maintenance_table is not None
    assert event_index is not None


def test_pipeline_state_connect_closes_connection(tmp_path):
    store = PipelineStateStore(tmp_path / "state.sqlite")

    with store._connect() as connection:
        connection.execute("select 1").fetchone()

    try:
        connection.execute("select 1")
    except Exception as exc:
        assert exc.__class__.__name__ == "ProgrammingError"
    else:
        raise AssertionError("PipelineStateStore._connect() left a sqlite connection open.")


def test_legacy_state_store_alias_remains_compatible():
    assert OcrStateStore is PipelineStateStore


def test_pipeline_state_schema_creates_core_tables(tmp_path):
    store = PipelineStateStore(tmp_path / "state.sqlite")

    with store._connect() as connection:
        rows = connection.execute(
            """
            select name
            from sqlite_master
            where type = 'table'
            """
        ).fetchall()

    tables = {row["name"] for row in rows}
    assert {"ocr_jobs", "html_jobs", "metadata_jobs", "full_runs"} <= tables


def test_pipeline_state_repository_facades_delegate_to_store(tmp_path):
    store = PipelineStateStore(tmp_path / "state.sqlite")

    run = store.full_runs.create(options={"mode": "ingest"})
    updated = store.full_runs.update(
        run_id=str(run["run_id"]),
        phase="testing",
        event="test_event",
        message="Facade event.",
    )

    assert updated is not None
    assert store.full_runs.latest()["run_id"] == run["run_id"]
    assert store.full_runs.get(str(run["run_id"]))["phase"] == "testing"
    assert store.full_runs.events(str(run["run_id"]), limit=1)[0]["event"] == "test_event"
    assert store.ocr_jobs.summary()["queued"] == 0
    assert store.html_jobs.summary()["queued"] == 0
    assert store.metadata_jobs.summary(job_type="enrich")["queued"] == 0


def test_metadata_job_lease_is_unique_under_parallel_workers(tmp_path):
    source = tmp_path / "zotero.sqlite"
    source.write_bytes(b"state")
    signature = FileSignature.from_path(source)
    store = PipelineStateStore(tmp_path / "state.sqlite")

    for index in range(20):
        created = store.enqueue_metadata_job(
            job_type="full_text",
            library_id="LIB1",
            attachment_key=f"PARENT{index}",
            data_dir=tmp_path,
            source_path=source,
            signature=signature,
            status="queued",
            reason="test",
            parent_item_key=f"PARENT{index}",
            parent_version=1,
            queue_key="full-text-v1",
        )
        assert created["created"] is True

    def lease_until_empty(worker: int) -> list[str]:
        job_ids = []
        while True:
            job = store.lease_next_metadata_job(
                job_type="full_text",
                owner=f"worker-{worker}",
                lease_seconds=60,
            )
            if job is None:
                return job_ids
            job_ids.append(str(job["job_id"]))

    with ThreadPoolExecutor(max_workers=4) as executor:
        leased_by_worker = list(executor.map(lease_until_empty, range(4)))

    job_ids = [job_id for worker_jobs in leased_by_worker for job_id in worker_jobs]
    assert len(job_ids) == 20
    assert len(set(job_ids)) == 20
    assert store.metadata_queue_summary(job_type="full_text")["queued"] == 0
    assert store.metadata_queue_summary(job_type="full_text")["running"] == 20


def test_stale_metadata_owner_cannot_complete_reclaimed_job(tmp_path):
    source = tmp_path / "zotero.sqlite"
    source.write_bytes(b"state")
    store = PipelineStateStore(tmp_path / "state.sqlite")
    created = store.enqueue_metadata_job(
        job_type="full_text",
        library_id="LIB1",
        attachment_key="PARENT1",
        data_dir=tmp_path,
        source_path=source,
        signature=FileSignature.from_path(source),
        status="queued",
        reason="test",
        parent_item_key="PARENT1",
        parent_version=1,
        queue_key="full-text-v1",
    )
    job_id = str(created["job_id"])
    first = store.lease_next_metadata_job(
        job_type="full_text", owner="old-owner", lease_seconds=60
    )
    assert first is not None
    with store._connect() as connection:
        connection.execute(
            "update metadata_jobs set leased_until = ? where job_id = ?",
            ("2000-01-01T00:00:00+00:00", job_id),
        )
    assert store.recover_expired_metadata_jobs(job_type="full_text") == 1
    second = store.lease_next_metadata_job(
        job_type="full_text", owner="new-owner", lease_seconds=60
    )
    assert second is not None
    before_heartbeat = str(second["leased_until"])

    stale_heartbeat = store.heartbeat_metadata_job(
        job_id=job_id,
        owner="old-owner",
        lease_seconds=600,
    )
    stale_completion = store.mark_metadata_job_succeeded(
        job_id=job_id,
        message="old worker finished late",
        owner="old-owner",
    )
    heartbeat = store.heartbeat_metadata_job(
        job_id=job_id,
        owner="new-owner",
        lease_seconds=600,
    )
    completed = store.mark_metadata_job_succeeded(
        job_id=job_id,
        message="new worker finished",
        owner="new-owner",
    )

    assert stale_heartbeat is False
    assert stale_completion["status"] == "running"
    assert stale_completion["lease_owner"] == "new-owner"
    assert heartbeat is True
    refreshed = store.get_metadata_job(job_id)
    assert refreshed is not None
    assert str(refreshed["leased_until"]) > before_heartbeat
    assert completed["status"] == "succeeded"
    with store._connect() as connection:
        events = [
            str(row["event"])
            for row in connection.execute(
                "select event from metadata_job_events where job_id = ? order by event_id",
                (job_id,),
            ).fetchall()
        ]
    assert "stale_completion_discarded" in events
    assert events.count("succeeded") == 1


def test_html_job_lease_is_unique_under_parallel_workers(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF")
    store = PipelineStateStore(tmp_path / "state.sqlite")
    store.enqueue_html_job(
        library_id="LIB1",
        attachment_key="PDF1",
        data_dir=tmp_path,
        source_path=source,
        signature=FileSignature.from_path(source),
        collection_key="direct_pdf",
        status="queued",
        reason="test",
    )

    def lease(owner: str):
        return store.lease_next_html_job(owner=owner, lease_seconds=60)

    with ThreadPoolExecutor(max_workers=4) as executor:
        leases = list(executor.map(lease, [f"worker-{index}" for index in range(4)]))

    leased = [job for job in leases if job is not None]
    assert len(leased) == 1
    assert leased[0]["status"] == "running"


def test_stale_html_owner_cannot_complete_reclaimed_job(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF")
    store = PipelineStateStore(tmp_path / "state.sqlite")
    created = store.enqueue_html_job(
        library_id="LIB1",
        attachment_key="PDF1",
        data_dir=tmp_path,
        source_path=source,
        signature=FileSignature.from_path(source),
        collection_key="direct_pdf",
        status="queued",
        reason="test",
    )
    job_id = str(created["job_id"])
    first = store.lease_next_html_job(owner="old-owner", lease_seconds=60)
    assert first is not None
    with store._connect() as connection:
        connection.execute(
            "update html_jobs set leased_until = ? where job_id = ?",
            ("2000-01-01T00:00:00+00:00", job_id),
        )
    assert store.recover_expired_html_jobs() == 1
    second = store.lease_next_html_job(owner="new-owner", lease_seconds=60)
    assert second is not None
    before_heartbeat = str(second["leased_until"])

    stale_heartbeat = store.heartbeat_html_job(
        job_id=job_id,
        owner="old-owner",
        lease_seconds=600,
    )
    stale_completion = store.mark_html_job_succeeded(
        job_id=job_id,
        message="old worker finished late",
        owner="old-owner",
    )
    heartbeat = store.heartbeat_html_job(
        job_id=job_id,
        owner="new-owner",
        lease_seconds=600,
    )
    completed = store.mark_html_job_succeeded(
        job_id=job_id,
        message="new worker finished",
        owner="new-owner",
    )

    assert stale_heartbeat is False
    assert stale_completion["status"] == "running"
    assert stale_completion["lease_owner"] == "new-owner"
    assert heartbeat is True
    refreshed = store.get_html_job(job_id)
    assert refreshed is not None
    assert str(refreshed["leased_until"]) > before_heartbeat
    assert completed["status"] == "succeeded"
    with store._connect() as connection:
        events = [
            str(row["event"])
            for row in connection.execute(
                "select event from html_job_events where job_id = ? order by event_id",
                (job_id,),
            ).fetchall()
        ]
    assert "stale_completion_discarded" in events
    assert events.count("succeeded") == 1


def test_html_attempt_budget_requires_explicit_reset(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF")
    store = PipelineStateStore(tmp_path / "state.sqlite")
    created = store.enqueue_html_job(
        library_id="LIB1",
        attachment_key="PDF1",
        data_dir=tmp_path,
        source_path=source,
        signature=FileSignature.from_path(source),
        collection_key="direct_pdf",
        status="queued",
        reason="test",
        max_attempts=1,
    )
    job_id = str(created["job_id"])
    leased = store.lease_next_html_job(owner="worker-1", lease_seconds=60)
    assert leased is not None
    failed = store.mark_html_job_failed(
        job_id=job_id,
        message="transient",
        retryable=True,
        owner="worker-1",
    )
    assert failed["status"] == "failed_final"

    rejected = store.retry_html_job(job_id)
    assert rejected["status"] == "failed_final"
    assert store.lease_next_html_job(owner="worker-2", lease_seconds=60) is None
    reset = store.retry_html_job(job_id, reset_attempts=True)
    assert reset["status"] == "queued"
    assert reset["attempts"] == 0
    next_generation = store.lease_next_html_job(owner="worker-2", lease_seconds=60)
    assert next_generation is not None
    assert next_generation["attempts"] == 1


def test_html_zero_max_attempts_is_unlimited(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF")
    store = PipelineStateStore(tmp_path / "state.sqlite")
    created = store.enqueue_html_job(
        library_id="LIB1",
        attachment_key="PDF1",
        data_dir=tmp_path,
        source_path=source,
        signature=FileSignature.from_path(source),
        collection_key="direct_pdf",
        status="queued",
        reason="test",
        max_attempts=0,
    )
    job_id = str(created["job_id"])
    first = store.lease_next_html_job(owner="worker-1", lease_seconds=60)
    assert first is not None
    failed = store.mark_html_job_failed(
        job_id=job_id,
        message="transient",
        retryable=True,
        owner="worker-1",
    )
    assert failed["status"] == "failed_retryable"
    retried = store.retry_html_job(job_id)
    assert retried["status"] == "queued"
    second = store.lease_next_html_job(owner="worker-2", lease_seconds=60)
    assert second is not None
    assert second["attempts"] == 2


def test_ocr_job_lease_is_unique_under_parallel_workers(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF")
    store = PipelineStateStore(tmp_path / "state.sqlite")
    store.enqueue_job(
        library_id="LIB1",
        attachment_key="PDF1",
        data_dir=tmp_path,
        source_path=source,
        signature=FileSignature.from_path(source),
        status="queued",
        reason="test",
    )

    def lease(owner: str):
        return store.lease_next_job(owner=owner, lease_seconds=60)

    with ThreadPoolExecutor(max_workers=4) as executor:
        leases = list(executor.map(lease, [f"worker-{index}" for index in range(4)]))

    leased = [job for job in leases if job is not None]
    assert len(leased) == 1
    assert leased[0]["status"] == "running"


def test_stale_ocr_owner_cannot_update_reclaimed_job(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF")
    store = PipelineStateStore(tmp_path / "state.sqlite")
    created = store.enqueue_job(
        library_id="LIB1",
        attachment_key="PDF1",
        data_dir=tmp_path,
        source_path=source,
        signature=FileSignature.from_path(source),
        status="queued",
        reason="test",
    )
    job_id = str(created["job_id"])
    first = store.lease_next_job(owner="old-owner", lease_seconds=60)
    assert first is not None
    with store._connect() as connection:
        connection.execute(
            "update ocr_jobs set leased_until = ? where job_id = ?",
            ("2000-01-01T00:00:00+00:00", job_id),
        )
    assert store.recover_expired_jobs() == 1
    second = store.lease_next_job(owner="new-owner", lease_seconds=60)
    assert second is not None
    before_heartbeat = str(second["leased_until"])

    stale_progress = store.mark_job_progress(
        job_id=job_id,
        phase="late-old-progress",
        message="old worker still running",
        owner="old-owner",
        lease_seconds=600,
    )
    stale_completion = store.mark_job_succeeded(
        job_id=job_id,
        message="old worker finished late",
        owner="old-owner",
    )
    heartbeat = store.mark_job_progress(
        job_id=job_id,
        phase="new-progress",
        message="new worker heartbeat",
        owner="new-owner",
        lease_seconds=600,
    )
    completed = store.mark_job_succeeded(
        job_id=job_id,
        message="new worker finished",
        owner="new-owner",
    )

    assert stale_progress["status"] == "running"
    assert stale_progress["lease_owner"] == "new-owner"
    assert stale_completion["status"] == "running"
    assert stale_completion["lease_owner"] == "new-owner"
    assert heartbeat["phase"] == "new-progress"
    assert str(heartbeat["leased_until"]) > before_heartbeat
    assert completed["status"] == "succeeded"
    with store._connect() as connection:
        events = [
            str(row["event"])
            for row in connection.execute(
                "select event from ocr_job_events where job_id = ? order by event_id",
                (job_id,),
            ).fetchall()
        ]
    assert "stale_progress_discarded" in events
    assert "stale_completion_discarded" in events
    assert events.count("succeeded") == 1


def test_ocr_attempt_budget_requires_explicit_reset(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF")
    store = PipelineStateStore(tmp_path / "state.sqlite")
    created = store.enqueue_job(
        library_id="LIB1",
        attachment_key="PDF1",
        data_dir=tmp_path,
        source_path=source,
        signature=FileSignature.from_path(source),
        status="queued",
        reason="test",
        max_attempts=1,
    )
    job_id = str(created["job_id"])
    leased = store.lease_next_job(owner="worker-1", lease_seconds=60)
    assert leased is not None
    failed = store.mark_job_failed(
        job_id=job_id,
        message="transient",
        retryable=True,
        owner="worker-1",
    )
    assert failed["status"] == "failed_final"

    rejected = store.retry_job(job_id)
    assert rejected["status"] == "failed_final"
    assert store.lease_next_job(owner="worker-2", lease_seconds=60) is None
    reset = store.retry_job(job_id, reset_attempts=True)
    assert reset["status"] == "queued"
    assert reset["attempts"] == 0
    next_generation = store.lease_next_job(owner="worker-2", lease_seconds=60)
    assert next_generation is not None
    assert next_generation["attempts"] == 1


def test_ocr_zero_max_attempts_is_unlimited(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF")
    store = PipelineStateStore(tmp_path / "state.sqlite")
    created = store.enqueue_job(
        library_id="LIB1",
        attachment_key="PDF1",
        data_dir=tmp_path,
        source_path=source,
        signature=FileSignature.from_path(source),
        status="queued",
        reason="test",
        max_attempts=0,
    )
    job_id = str(created["job_id"])
    first = store.lease_next_job(owner="worker-1", lease_seconds=60)
    assert first is not None
    failed = store.mark_job_failed(
        job_id=job_id,
        message="transient",
        retryable=True,
        owner="worker-1",
    )
    assert failed["status"] == "failed_retryable"
    retried = store.retry_job(job_id)
    assert retried["status"] == "queued"
    second = store.lease_next_job(owner="worker-2", lease_seconds=60)
    assert second is not None
    assert second["attempts"] == 2


def test_recover_orphaned_metadata_jobs_returns_running_jobs_to_queue(tmp_path):
    source = tmp_path / "zotero.sqlite"
    source.write_bytes(b"state")
    signature = FileSignature.from_path(source)
    store = PipelineStateStore(tmp_path / "state.sqlite")
    created = store.enqueue_metadata_job(
        job_type="full_text",
        library_id="LIB1",
        attachment_key="PARENT1",
        data_dir=tmp_path,
        source_path=source,
        signature=signature,
        status="queued",
        reason="test",
        parent_item_key="PARENT1",
        parent_version=1,
        queue_key="full-text-v1",
    )
    leased = store.lease_next_metadata_job(
        job_type="full_text",
        owner="worker-a",
        lease_seconds=3600,
    )

    kept = store.recover_orphaned_metadata_jobs(
        job_type="full_text",
        owner_alive=lambda owner: owner == "worker-a",
    )
    kept_by_default = store.recover_orphaned_metadata_jobs(job_type="full_text")
    recovered = store.recover_orphaned_metadata_jobs(
        job_type="full_text",
        owner_alive=lambda _owner: False,
    )
    job = store.get_metadata_job(str(created["job_id"]))

    assert leased is not None
    assert kept == 0
    assert kept_by_default == 0
    assert recovered == 1
    assert job["status"] == "queued"
    assert job["phase"] == "recovered"
    assert job["lease_owner"] is None
    assert job["leased_until"] is None
    assert job["last_error"] == "Recovered orphaned metadata job lease."


def test_recover_orphaned_metadata_jobs_recovers_dead_pid_by_default(tmp_path):
    source = tmp_path / "zotero.sqlite"
    source.write_bytes(b"state")
    signature = FileSignature.from_path(source)
    store = PipelineStateStore(tmp_path / "state.sqlite")
    created = store.enqueue_metadata_job(
        job_type="full_text",
        library_id="LIB1",
        attachment_key="PARENT1",
        data_dir=tmp_path,
        source_path=source,
        signature=signature,
        status="queued",
        reason="test",
        parent_item_key="PARENT1",
        parent_version=1,
        queue_key="full-text-v1",
    )
    leased = store.lease_next_metadata_job(
        job_type="full_text",
        owner=f"zotero-worker-metadata:test:{os.getpid()}",
        lease_seconds=3600,
    )

    kept = store.recover_orphaned_metadata_jobs(job_type="full_text")
    with store._connect() as connection:
        connection.execute(
            """
            update metadata_jobs
            set lease_owner = ?
            where job_id = ?
            """,
            ("zotero-worker-metadata:test:99999999", created["job_id"]),
        )
    recovered = store.recover_orphaned_metadata_jobs(job_type="full_text")
    job = store.get_metadata_job(str(created["job_id"]))

    assert leased is not None
    assert kept == 0
    assert recovered == 1
    assert job["status"] == "queued"
    assert job["last_error"] == "Recovered orphaned metadata job lease."


def test_metadata_queue_summary_counts_transient_failed_final(tmp_path):
    source = tmp_path / "zotero.sqlite"
    source.write_bytes(b"state")
    signature = FileSignature.from_path(source)
    store = PipelineStateStore(tmp_path / "state.sqlite")

    transient = store.enqueue_metadata_job(
        job_type="full_text",
        library_id="LIB1",
        attachment_key="PARENT1",
        data_dir=tmp_path,
        source_path=source,
        signature=signature,
        status="queued",
        reason="test",
        parent_item_key="PARENT1",
        parent_version=1,
        queue_key="full-text-v1",
        max_attempts=1,
    )
    permanent = store.enqueue_metadata_job(
        job_type="full_text",
        library_id="LIB1",
        attachment_key="PARENT2",
        data_dir=tmp_path,
        source_path=source,
        signature=signature,
        status="queued",
        reason="test",
        parent_item_key="PARENT2",
        parent_version=1,
        queue_key="full-text-v1",
        max_attempts=1,
    )
    other_type = store.enqueue_metadata_job(
        job_type="enrich",
        library_id="LIB1",
        attachment_key="PARENT3",
        data_dir=tmp_path,
        source_path=source,
        signature=signature,
        status="queued",
        reason="test",
        parent_item_key="PARENT3",
        parent_version=1,
        queue_key="enrich-v1",
        max_attempts=1,
    )

    for job_type, job, message, retryable in (
        ("full_text", transient, "HTTP 429: too many requests", True),
        ("full_text", permanent, "No matching full text candidate found", False),
        ("enrich", other_type, "HTTP 503: temporarily unavailable", True),
    ):
        leased = store.lease_next_metadata_job(
            job_type=job_type,
            owner="worker-a",
            lease_seconds=60,
        )
        assert leased is not None
        assert leased["job_id"] == job["job_id"]
        failed = store.mark_metadata_job_failed(
            job_id=str(job["job_id"]),
            message=message,
            retryable=retryable,
        )
        assert failed["status"] == "failed_final"

    full_text = store.metadata_queue_summary(job_type="full_text")
    enrich = store.metadata_queue_summary(job_type="enrich")
    total = store.metadata_queue_summary()

    assert full_text["failed_final"] == 2
    assert full_text["failed_transient"] == 1
    assert enrich["failed_final"] == 1
    assert enrich["failed_transient"] == 1
    assert total["failed_final"] == 3
    assert total["failed_transient"] == 2


def test_metadata_queue_summary_and_listing_are_scoped_by_library(tmp_path):
    source = tmp_path / "zotero.sqlite"
    source.write_bytes(b"state")
    signature = FileSignature.from_path(source)
    store = PipelineStateStore(tmp_path / "state.sqlite")

    created = {}
    for library_id, attachment_key in (
        ("LIB2", "PARENT-OTHER"),
        ("LIB1", "PARENT-TARGET"),
        ("LIB1", "PARENT-TARGET-2"),
    ):
        created[library_id, attachment_key] = store.enqueue_metadata_job(
            job_type="full_text",
            library_id=library_id,
            attachment_key=attachment_key,
            data_dir=tmp_path,
            source_path=source,
            signature=signature,
            status="queued",
            reason="test",
            parent_item_key=attachment_key,
            parent_version=1,
            queue_key="full-text-v1",
        )

    with store._connect() as connection:
        connection.execute(
            """
            update metadata_jobs
            set status = 'failed_final', last_error = 'HTTP 429: too many requests'
            where job_id = ?
            """,
            (created["LIB2", "PARENT-OTHER"]["job_id"],),
        )

    scoped_summary = store.metadata_queue_summary(
        job_type="full_text",
        library_ids={"LIB1"},
    )
    scoped_jobs = store.list_metadata_jobs(
        job_type="full_text",
        statuses={"queued"},
        library_ids={"LIB1"},
        limit=1,
    )
    second_scoped_job = store.list_metadata_jobs(
        job_type="full_text",
        statuses={"queued"},
        library_ids={"LIB1"},
        limit=1,
        offset=1,
    )
    global_summary = store.metadata_queue_summary(job_type="full_text")

    assert scoped_summary["queued"] == 2
    assert scoped_summary["library_ids"] == ["LIB1"]
    assert scoped_summary["failed_transient"] == 0
    assert len(scoped_jobs) == 1
    assert scoped_jobs[0]["library_id"] == "LIB1"
    assert second_scoped_job[0]["library_id"] == "LIB1"
    assert second_scoped_job[0]["job_id"] != scoped_jobs[0]["job_id"]
    assert global_summary["queued"] == 2
    assert global_summary["failed_final"] == 1
    assert global_summary["failed_transient"] == 1


def test_large_terminal_metadata_result_is_compacted_without_changing_return_value(tmp_path):
    source = tmp_path / "zotero.sqlite"
    source.write_bytes(b"state")
    store = PipelineStateStore(tmp_path / "state.sqlite")
    created = store.enqueue_metadata_job(
        job_type="full_text",
        library_id="LIB1",
        attachment_key="PARENT-LARGE",
        data_dir=tmp_path,
        source_path=source,
        signature=FileSignature.from_path(source),
        status="queued",
        reason="test",
        parent_item_key="PARENT-LARGE",
        queue_key="full-text-v1",
    )
    downstream_ref = {
        "ok": True,
        "classification": "downstream_orchestrator",
        "stage": "pdf_html",
        "reason": "full_text_pdf_found",
        "attachment": {
            "library_id": "LIB1",
            "data_dir": str(tmp_path),
            "storage_dir": str(tmp_path / "storage"),
            "key": "PDF-LARGE",
            "item_id": 1,
            "parent_item_id": 2,
            "date_modified": None,
            "link_mode": 0,
            "content_type": "application/pdf",
            "zotero_path": "storage:paper.pdf",
            "file_path": str(tmp_path / "storage" / "PDF-LARGE" / "paper.pdf"),
            "parent_key": "PARENT-LARGE",
        },
    }
    payload = {
        "worker_status": "pdf_found",
        "provider_events": [{"raw": "Ж" * 100_000}],
        "existing_pdf_enqueue": downstream_ref,
        "repeated_downstream_reference": downstream_ref,
    }
    expected_json = json.dumps(payload, ensure_ascii=False)

    completed = store.mark_metadata_job_succeeded(
        job_id=str(created["job_id"]),
        message="done",
        result=payload,
    )
    stored = store.get_metadata_job(str(created["job_id"]))

    assert completed["result_json"] == expected_json
    assert stored is not None
    assert stored["result_json"] != expected_json
    assert len(str(stored["result_json"]).encode("utf-8")) <= 64 * 1024
    compacted = json.loads(str(stored["result_json"]))
    assert compacted["_compacted"]["original_bytes"] == len(expected_json.encode("utf-8"))
    assert compacted["summary"]["worker_status"] == "pdf_found"
    assert compacted["downstream_refs"] == [downstream_ref, downstream_ref]
    assert (
        compacted["summary"]["existing_pdf_enqueue"]["_type"]
        == "downstream_orchestrator_ref"
    )


def test_metadata_result_history_compaction_is_bounded_and_reports_backlog(tmp_path):
    source = tmp_path / "zotero.sqlite"
    source.write_bytes(b"state")
    store = PipelineStateStore(tmp_path / "state.sqlite")
    job_ids = []
    raw = json.dumps({"candidate": {"raw": "x" * 50_000}})
    for index in range(2):
        created = store.enqueue_metadata_job(
            job_type="enrich",
            library_id="LIB1",
            attachment_key=f"PARENT-{index}",
            data_dir=tmp_path,
            source_path=source,
            signature=FileSignature.from_path(source),
            status="queued",
            reason="test",
            parent_item_key=f"PARENT-{index}",
            queue_key="enrich-v1",
        )
        job_ids.append(str(created["job_id"]))
    with store._connect() as connection:
        connection.executemany(
            "update metadata_jobs set status = 'succeeded', result_json = ? where job_id = ?",
            [(raw, job_id) for job_id in job_ids],
        )

    first = store.compact_metadata_result_history(
        max_result_bytes=4_096,
        batch_size=1,
        max_batches=1,
    )
    second = store.compact_metadata_result_history(
        max_result_bytes=4_096,
        batch_size=10,
        max_batches=1,
    )

    assert first["compacted_rows"] == 1
    assert first["backlog_remaining"] is True
    assert first["logical_bytes_reclaimed"] > 0
    assert second["compacted_rows"] == 1
    assert second["backlog_remaining"] is False
    assert second["after"]["compacted_rows"] == 2


def test_event_history_pruning_is_bounded_per_entity(tmp_path):
    store = PipelineStateStore(tmp_path / "state.sqlite")
    run = store.create_full_run(options={"mode": "retention-test"})
    old = "2000-01-01T00:00:00+00:00"
    recent = "2999-01-01T00:00:00+00:00"
    with store._connect() as connection:
        for created_at in [old] * 7 + [recent]:
            connection.execute(
                """
                insert into full_run_events (run_id, event, message, metadata, created_at)
                values (?, 'event', '', null, ?)
                """,
                (run["run_id"], created_at),
            )

    first = store.prune_event_history(
        retention_days=1,
        keep_per_entity=1,
        batch_size=2,
        max_batches=1,
    )
    second = store.prune_event_history(
        retention_days=1,
        keep_per_entity=1,
        batch_size=10,
        max_batches=1,
    )

    assert first["deleted"]["full_run_events"] == 2
    assert first["has_more"]["full_run_events"] is True
    assert first["backlog_remaining"] is True
    assert second["deleted"]["full_run_events"] == 5
    assert second["has_more"]["full_run_events"] is False
    assert second["counts_after"]["full_run_events"] == 2
