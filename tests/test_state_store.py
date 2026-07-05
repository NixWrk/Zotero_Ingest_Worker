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
