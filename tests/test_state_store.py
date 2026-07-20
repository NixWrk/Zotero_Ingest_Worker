import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from zotero_ingest_worker.state import (
    FileSignature,
    PIPELINE_STATE_SCHEMA_VERSION,
    OcrStateStore,
    PipelineStateStore,
)


def test_pipeline_state_store_sets_schema_version(tmp_path):
    store = PipelineStateStore(tmp_path / "state.sqlite")

    assert store.schema_version() == PIPELINE_STATE_SCHEMA_VERSION


def test_pipeline_state_store_rejects_newer_schema_before_mutation(tmp_path):
    path = tmp_path / "state.sqlite"
    store = PipelineStateStore(path)
    future_version = PIPELINE_STATE_SCHEMA_VERSION + 1
    with store._connect() as connection:
        connection.execute(f"pragma user_version = {future_version}")

    with pytest.raises(RuntimeError, match="newer than supported"):
        PipelineStateStore(path)

    with store._connect() as connection:
        actual_version = int(connection.execute("pragma user_version").fetchone()[0])
    assert actual_version == future_version


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
        raise AssertionError(
            "PipelineStateStore._connect() left a sqlite connection open."
        )


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
    assert (
        store.full_runs.events(str(run["run_id"]), limit=1)[0]["event"] == "test_event"
    )
    assert store.ocr_jobs.summary()["queued"] == 0
    assert store.html_jobs.summary()["queued"] == 0
    assert store.metadata_jobs.summary(job_type="enrich")["queued"] == 0


def test_metadata_enqueue_is_atomic_under_parallel_duplicate_requests(tmp_path):
    store = PipelineStateStore(tmp_path / "state.sqlite")
    source = tmp_path / "zotero.sqlite"
    source.write_bytes(b"sqlite")
    signature = FileSignature.from_path(source)
    workers = 8
    barrier = threading.Barrier(workers)
    local = threading.local()
    original_lookup = store.get_metadata_job_by_dedupe_key

    def synchronized_initial_lookup(dedupe_key: str):
        result = original_lookup(dedupe_key)
        calls = int(getattr(local, "calls", 0))
        local.calls = calls + 1
        if calls == 0:
            barrier.wait(timeout=5)
        return result

    store.get_metadata_job_by_dedupe_key = synchronized_initial_lookup  # type: ignore[method-assign]

    def enqueue() -> dict[str, object]:
        return store.enqueue_metadata_job(
            job_type="full_text",
            library_id="LIB1",
            attachment_key="PARENT1",
            data_dir=tmp_path,
            source_path=source,
            signature=signature,
            status="queued",
            reason="parallel-test",
            parent_item_key="PARENT1",
            parent_version=1,
            queue_key="full-text-v1",
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(lambda _index: enqueue(), range(workers)))

    assert sum(result["created"] is True for result in results) == 1
    assert len({str(result["job_id"]) for result in results}) == 1
    assert len(store.list_metadata_jobs(job_type="full_text", limit=None)) == 1


@pytest.mark.parametrize("queue_name", ["ocr", "html"])
def test_attachment_enqueue_is_atomic_under_parallel_duplicate_requests(
    tmp_path, queue_name
):
    store = PipelineStateStore(tmp_path / "state.sqlite")
    source = tmp_path / ("paper.pdf" if queue_name == "ocr" else "paper.html")
    source.write_bytes(b"content")
    signature = FileSignature.from_path(source)
    workers = 8
    barrier = threading.Barrier(workers)
    local = threading.local()
    lookup_name = (
        "get_job_by_dedupe_key" if queue_name == "ocr" else "get_html_job_by_dedupe_key"
    )
    original_lookup = getattr(store, lookup_name)

    def synchronized_initial_lookup(dedupe_key: str):
        result = original_lookup(dedupe_key)
        calls = int(getattr(local, "calls", 0))
        local.calls = calls + 1
        if calls == 0:
            barrier.wait(timeout=5)
        return result

    setattr(store, lookup_name, synchronized_initial_lookup)

    def enqueue() -> dict[str, object]:
        common = {
            "library_id": "LIB1",
            "attachment_key": "ATTACHMENT1",
            "data_dir": tmp_path,
            "source_path": source,
            "signature": signature,
            "status": "queued",
            "reason": "parallel-test",
        }
        if queue_name == "ocr":
            return store.enqueue_job(**common)
        return store.enqueue_html_job(
            **common,
            collection_key="direct_pdf",
            pipeline_key="direct-pdf-v1",
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(lambda _index: enqueue(), range(workers)))

    assert sum(result["created"] is True for result in results) == 1
    assert len({str(result["job_id"]) for result in results}) == 1
    listed = (
        store.list_jobs(limit=100)
        if queue_name == "ocr"
        else store.list_html_jobs(limit=100)
    )
    assert len(listed) == 1


@pytest.mark.parametrize("queue_name", ["ocr", "html", "metadata"])
def test_forced_enqueue_starts_a_distinct_generation(tmp_path, queue_name):
    store = PipelineStateStore(tmp_path / "state.sqlite")
    source = tmp_path / f"{queue_name}.source"
    source.write_bytes(b"content")
    common = {
        "library_id": "LIB1",
        "attachment_key": "ATTACHMENT1",
        "data_dir": tmp_path,
        "source_path": source,
        "signature": FileSignature.from_path(source),
        "status": "queued",
        "reason": "forced-generation-test",
        "force": True,
    }

    if queue_name == "ocr":
        first = store.enqueue_job(**common)
        second = store.enqueue_job(**common)
        listed = store.list_jobs(limit=100)
    elif queue_name == "html":
        first = store.enqueue_html_job(
            **common,
            collection_key="direct_pdf",
            pipeline_key="direct-pdf-v1",
        )
        second = store.enqueue_html_job(
            **common,
            collection_key="direct_pdf",
            pipeline_key="direct-pdf-v1",
        )
        listed = store.list_html_jobs(limit=100)
    else:
        first = store.enqueue_metadata_job(
            **common,
            job_type="full_text",
            queue_key="full-text-v1",
        )
        second = store.enqueue_metadata_job(
            **common,
            job_type="full_text",
            queue_key="full-text-v1",
        )
        listed = store.list_metadata_jobs(job_type="full_text", limit=None)

    assert first["created"] is True
    assert second["created"] is True
    assert first["job_id"] != second["job_id"]
    assert len(listed) == 2


@pytest.mark.parametrize(
    ("method_name", "extra"),
    [
        ("lease_next_job", {}),
        ("lease_next_html_job", {}),
        ("lease_next_metadata_job", {"job_type": "full_text"}),
    ],
)
@pytest.mark.parametrize("lease_seconds", [0, -1, True, 1.5, "60", 604_801])
def test_queue_claim_rejects_invalid_lease_contract(
    tmp_path, method_name, extra, lease_seconds
):
    store = PipelineStateStore(tmp_path / "state.sqlite")

    with pytest.raises(ValueError, match="lease_seconds"):
        getattr(store, method_name)(
            owner="worker",
            lease_seconds=lease_seconds,
            **extra,
        )


@pytest.mark.parametrize(
    ("method_name", "extra"),
    [
        ("lease_next_job", {}),
        ("lease_next_html_job", {}),
        ("lease_next_metadata_job", {"job_type": "full_text"}),
    ],
)
@pytest.mark.parametrize("owner", [None, "", "   ", 7])
def test_queue_claim_rejects_invalid_owner_contract(
    tmp_path, method_name, extra, owner
):
    store = PipelineStateStore(tmp_path / "state.sqlite")

    with pytest.raises(ValueError, match="owner"):
        getattr(store, method_name)(
            owner=owner,
            lease_seconds=60,
            **extra,
        )


@pytest.mark.parametrize("queue_name", ["ocr", "html", "metadata"])
@pytest.mark.parametrize(
    "max_attempts",
    [True, -1, 1.5, "3", 2_147_483_648],
)
def test_queue_enqueue_rejects_invalid_max_attempts(
    tmp_path,
    queue_name,
    max_attempts,
):
    store = PipelineStateStore(tmp_path / "state.sqlite")
    source = tmp_path / f"{queue_name}.source"
    source.write_bytes(b"content")
    common = {
        "library_id": "LIB1",
        "attachment_key": "ATTACHMENT1",
        "data_dir": tmp_path,
        "source_path": source,
        "signature": FileSignature.from_path(source),
        "status": "queued",
        "reason": "attempt-budget-contract-test",
        "max_attempts": max_attempts,
    }

    with pytest.raises(ValueError, match="max_attempts"):
        if queue_name == "ocr":
            store.enqueue_job(**common)
        elif queue_name == "html":
            store.enqueue_html_job(**common, collection_key="direct_pdf")
        else:
            store.enqueue_metadata_job(job_type="full_text", **common)


@pytest.mark.parametrize("queue_name", ["ocr", "html", "metadata"])
def test_queue_enqueue_accepts_zero_as_unlimited_attempt_budget(tmp_path, queue_name):
    store = PipelineStateStore(tmp_path / "state.sqlite")
    source = tmp_path / f"{queue_name}.source"
    source.write_bytes(b"content")
    common = {
        "library_id": "LIB1",
        "attachment_key": "ATTACHMENT1",
        "data_dir": tmp_path,
        "source_path": source,
        "signature": FileSignature.from_path(source),
        "status": "queued",
        "reason": "unlimited-attempt-budget-test",
        "max_attempts": 0,
    }
    if queue_name == "ocr":
        created = store.enqueue_job(**common)
    elif queue_name == "html":
        created = store.enqueue_html_job(**common, collection_key="direct_pdf")
    else:
        created = store.enqueue_metadata_job(job_type="full_text", **common)
    assert created["max_attempts"] == 0


@pytest.mark.parametrize("attempt", [True, 0, -1, 1.5, "1"])
def test_metadata_attempt_token_rejects_invalid_values(tmp_path, attempt):
    store = PipelineStateStore(tmp_path / "state.sqlite")
    source = tmp_path / "zotero.sqlite"
    source.write_bytes(b"sqlite")
    created = store.enqueue_metadata_job(
        job_type="full_text",
        library_id="LIB1",
        attachment_key="PARENT1",
        data_dir=tmp_path,
        source_path=source,
        signature=FileSignature.from_path(source),
        status="queued",
        reason="attempt-contract-test",
    )
    leased = store.lease_next_metadata_job(
        job_type="full_text",
        owner="worker",
        lease_seconds=60,
    )
    assert leased is not None

    with pytest.raises(ValueError, match="attempt"):
        store.heartbeat_metadata_job(
            job_id=str(created["job_id"]),
            owner="worker",
            lease_seconds=60,
            attempt=attempt,
        )
    with pytest.raises(ValueError, match="attempt"):
        store.mark_metadata_job_succeeded(
            job_id=str(created["job_id"]),
            owner="worker",
            attempt=attempt,
            message="invalid attempt",
        )
    current = store.get_metadata_job(str(created["job_id"]))
    assert current is not None
    assert current["status"] == "running"


def test_expired_ocr_lease_cannot_complete_or_extend_progress(tmp_path):
    store = PipelineStateStore(tmp_path / "state.sqlite")
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF test")
    created = store.enqueue_job(
        library_id="LIB1",
        attachment_key="OCR1",
        data_dir=tmp_path,
        source_path=source,
        signature=FileSignature.from_path(source),
        status="queued",
        reason="expired-lease-test",
    )
    job_id = str(created["job_id"])
    leased = store.lease_next_job(owner="worker", lease_seconds=60)
    assert leased is not None
    with store._connect() as connection:
        connection.execute(
            "update ocr_jobs set leased_until = ? where job_id = ?",
            ("2000-01-01T00:00:00+00:00", job_id),
        )

    completion = store.mark_job_succeeded(
        job_id=job_id,
        owner="worker",
        message="too late",
    )
    progress = store.mark_job_progress(
        job_id=job_id,
        owner="worker",
        phase="publishing",
        message="too late",
        lease_seconds=60,
    )

    assert completion["status"] == "running"
    assert progress["status"] == "running"
    assert progress["phase"] == "leased"
    assert progress["leased_until"] == "2000-01-01T00:00:00+00:00"


def test_state_json_rejects_non_finite_values_without_partial_mutation(tmp_path):
    store = PipelineStateStore(tmp_path / "state.sqlite")

    with pytest.raises(ValueError):
        store.create_full_run(options={"score": float("nan")})
    assert store.latest_full_run() is None

    source = tmp_path / "zotero.sqlite"
    source.write_bytes(b"sqlite")
    created = store.enqueue_metadata_job(
        job_type="full_text",
        library_id="LIB1",
        attachment_key="PARENT1",
        data_dir=tmp_path,
        source_path=source,
        signature=FileSignature.from_path(source),
        status="queued",
        reason="json-contract-test",
    )
    leased = store.lease_next_metadata_job(
        job_type="full_text",
        owner="worker",
        lease_seconds=60,
    )
    assert leased is not None

    with pytest.raises(ValueError):
        store.mark_metadata_job_succeeded(
            job_id=str(created["job_id"]),
            owner="worker",
            attempt=1,
            message="invalid JSON",
            result={"score": float("inf")},
        )
    current = store.get_metadata_job(str(created["job_id"]))
    assert current is not None
    assert current["status"] == "running"
    assert current["result_json"] is None


def test_metadata_string_result_is_stored_as_valid_json(tmp_path):
    store = PipelineStateStore(tmp_path / "state.sqlite")
    source = tmp_path / "zotero.sqlite"
    source.write_bytes(b"sqlite")
    created = store.enqueue_metadata_job(
        job_type="full_text",
        library_id="LIB1",
        attachment_key="PARENT1",
        data_dir=tmp_path,
        source_path=source,
        signature=FileSignature.from_path(source),
        status="queued",
        reason="json-string-test",
    )
    leased = store.lease_next_metadata_job(
        job_type="full_text",
        owner="worker",
        lease_seconds=60,
    )
    assert leased is not None

    completed = store.mark_metadata_job_succeeded(
        job_id=str(created["job_id"]),
        owner="worker",
        attempt=1,
        message="stored",
        result="plain text",
    )

    assert json.loads(str(completed["result_json"])) == "plain text"


@pytest.mark.parametrize(
    ("relay_result", "expected"),
    [
        (None, None),
        ({"ok": True}, "succeeded"),
        ({"ok": False}, "failed"),
        ({"ok": "true"}, "invalid"),
        ([], "invalid"),
    ],
)
def test_ocr_completion_classifies_relay_result_exactly(
    tmp_path, relay_result, expected
):
    store = PipelineStateStore(tmp_path / "state.sqlite")
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF test")
    created = store.enqueue_job(
        library_id="LIB1",
        attachment_key="OCR1",
        data_dir=tmp_path,
        source_path=source,
        signature=FileSignature.from_path(source),
        status="queued",
        reason="relay-contract-test",
    )
    leased = store.lease_next_job(owner="worker", lease_seconds=60)
    assert leased is not None

    completed = store.mark_job_succeeded(
        job_id=str(created["job_id"]),
        owner="worker",
        message="complete",
        relay_result=relay_result,
    )

    assert completed["relay_status"] == expected


def test_metadata_attempt_token_rejects_stale_completion_after_reclaim(tmp_path):
    store = PipelineStateStore(tmp_path / "state.sqlite")
    source = tmp_path / "zotero.sqlite"
    source.write_bytes(b"sqlite")
    created = store.enqueue_metadata_job(
        job_type="full_text",
        library_id="LIB1",
        attachment_key="PARENT1",
        data_dir=tmp_path,
        source_path=source,
        signature=FileSignature.from_path(source),
        status="queued",
        reason="attempt-token-test",
        parent_item_key="PARENT1",
        parent_version=1,
        queue_key="full-text-v1",
    )
    job_id = str(created["job_id"])
    first = store.lease_next_metadata_job(
        job_type="full_text",
        owner="shared-owner",
        lease_seconds=60,
    )
    assert first is not None
    assert first["attempts"] == 1
    with store._connect() as connection:
        connection.execute(
            "update metadata_jobs set leased_until = ? where job_id = ?",
            ("2000-01-01T00:00:00+00:00", job_id),
        )
    assert store.recover_expired_metadata_jobs(job_type="full_text") == 1
    second = store.lease_next_metadata_job(
        job_type="full_text",
        owner="shared-owner",
        lease_seconds=60,
    )
    assert second is not None
    assert second["attempts"] == 2

    stale = store.mark_metadata_job_succeeded(
        job_id=job_id,
        owner="shared-owner",
        attempt=1,
        message="stale attempt",
    )

    assert stale["status"] == "running"
    assert stale["attempts"] == 2
    current = store.mark_metadata_job_succeeded(
        job_id=job_id,
        owner="shared-owner",
        attempt=2,
        message="current attempt",
    )
    assert current["status"] == "succeeded"
    with store._connect() as connection:
        events = [
            str(row["event"])
            for row in connection.execute(
                "select event from metadata_job_events where job_id = ? order by event_id",
                (job_id,),
            ).fetchall()
        ]
    assert "stale_completion_discarded" in events


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
    gap_completion = store.mark_metadata_job_succeeded(
        job_id=job_id,
        message="old worker finished before re-lease",
        owner="old-owner",
    )
    assert gap_completion["status"] == "queued"
    assert gap_completion["lease_owner"] is None

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
    gap_completion = store.mark_html_job_succeeded(
        job_id=job_id,
        message="old worker finished before re-lease",
        owner="old-owner",
    )
    assert gap_completion["status"] == "queued"
    assert gap_completion["lease_owner"] is None

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

    with pytest.raises(
        ValueError,
        match="OCR job progress requires a non-empty lease owner",
    ):
        store.mark_job_progress(
            job_id=job_id,
            phase="unfenced",
            message="must fail closed",
        )
    queued = store.get_job(job_id)
    assert queued is not None
    assert queued["phase"] == "queued"

    first = store.lease_next_job(owner="old-owner", lease_seconds=60)
    assert first is not None
    with store._connect() as connection:
        connection.execute(
            "update ocr_jobs set leased_until = ? where job_id = ?",
            ("2000-01-01T00:00:00+00:00", job_id),
        )
    assert store.recover_expired_jobs() == 1
    gap_completion = store.mark_job_succeeded(
        job_id=job_id,
        message="old worker finished before re-lease",
        owner="old-owner",
    )
    assert gap_completion["status"] == "queued"
    assert gap_completion["lease_owner"] is None

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


def test_metadata_orphan_recovery_preserves_lease_heartbeated_during_probe(tmp_path):
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
    assert (
        store.lease_next_metadata_job(
            job_type="full_text",
            owner="worker",
            lease_seconds=60,
        )
        is not None
    )

    def heartbeat_during_probe(owner: str) -> bool:
        assert owner == "worker"
        assert store.heartbeat_metadata_job(
            job_id=job_id,
            owner=owner,
            lease_seconds=3600,
        )
        return False

    recovered = store.recover_orphaned_metadata_jobs(
        job_type="full_text",
        owner_alive=heartbeat_during_probe,
    )
    job = store.get_metadata_job(job_id)

    assert recovered == 0
    assert job is not None
    assert job["status"] == "running"
    assert job["lease_owner"] == "worker"


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


def test_large_terminal_metadata_result_is_compacted_without_changing_return_value(
    tmp_path,
):
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
    assert compacted["_compacted"]["original_bytes"] == len(
        expected_json.encode("utf-8")
    )
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


def test_retry_and_lease_clear_attempt_scoped_diagnostics(tmp_path):
    store = PipelineStateStore(tmp_path / "state.sqlite")

    html_source = tmp_path / "paper.html"
    html_source.write_text("<html>test</html>", encoding="utf-8")
    html = store.enqueue_html_job(
        library_id="LIB",
        attachment_key="HTML1",
        data_dir=tmp_path,
        source_path=html_source,
        signature=FileSignature.from_path(html_source),
        collection_key="direct_pdf",
        status="queued",
        reason="test",
    )
    metadata_source = tmp_path / "zotero.sqlite"
    metadata_source.write_bytes(b"state")
    metadata = store.enqueue_metadata_job(
        job_type="full_text",
        library_id="LIB",
        attachment_key="META1",
        data_dir=tmp_path,
        source_path=metadata_source,
        signature=FileSignature.from_path(metadata_source),
        status="queued",
        reason="test",
    )
    ocr_source = tmp_path / "paper.pdf"
    ocr_source.write_bytes(b"%PDF test")
    ocr = store.enqueue_job(
        library_id="LIB",
        attachment_key="OCR1",
        data_dir=tmp_path,
        source_path=ocr_source,
        signature=FileSignature.from_path(ocr_source),
        status="queued",
        reason="test",
    )

    ids = {
        "html_jobs": str(html["job_id"]),
        "metadata_jobs": str(metadata["job_id"]),
        "ocr_jobs": str(ocr["job_id"]),
    }
    with store._connect() as connection:
        for table, job_id in ids.items():
            connection.execute(
                f"update {table} set status = 'failed_retryable', phase = 'failed', "
                "last_error = ?, relay_status = ?, relay_result = ? where job_id = ?",
                ("old error", "failed", '{"old":true}', job_id),
            )

    retried = (
        store.retry_html_job(str(html["job_id"])),
        store.retry_metadata_job(str(metadata["job_id"])),
        store.retry_job(str(ocr["job_id"])),
    )
    for job in retried:
        assert job["status"] == "queued"
        assert job["last_error"] is None
        assert job["relay_status"] is None
        assert job["relay_result"] is None

    with store._connect() as connection:
        for table, job_id in ids.items():
            connection.execute(
                f"update {table} set last_error = ?, relay_status = ?, relay_result = ? "
                "where job_id = ?",
                ("recovery error", "failed", '{"old":true}', job_id),
            )

    leased = (
        store.lease_next_html_job(owner="worker", lease_seconds=60),
        store.lease_next_metadata_job(
            job_type="full_text", owner="worker", lease_seconds=60
        ),
        store.lease_next_job(owner="worker", lease_seconds=60),
    )
    for job in leased:
        assert job is not None
        assert job["last_error"] is None
        assert job["relay_status"] is None
        assert job["relay_result"] is None


def test_large_failed_metadata_result_is_compacted_without_changing_return_value(
    tmp_path,
):
    source = tmp_path / "zotero.sqlite"
    source.write_bytes(b"state")
    store = PipelineStateStore(tmp_path / "state.sqlite")
    created = store.enqueue_metadata_job(
        job_type="full_text",
        library_id="LIB1",
        attachment_key="PARENT-FAILED-LARGE",
        data_dir=tmp_path,
        source_path=source,
        signature=FileSignature.from_path(source),
        status="queued",
        reason="test",
        parent_item_key="PARENT-FAILED-LARGE",
        queue_key="full-text-v1",
    )
    leased = store.lease_next_metadata_job(
        job_type="full_text",
        owner="failure-owner",
        lease_seconds=60,
    )
    assert leased is not None
    payload = {
        "worker_status": "html_found",
        "relay_attachment": {
            "ok": False,
            "status": "local_copy_failed",
            "diagnostic": "Ж" * 100_000,
        },
    }
    expected_json = json.dumps(payload, ensure_ascii=False)
    relay_result = {"ok": False, "reason": "relay rejected attachment"}

    failed = store.mark_metadata_job_failed(
        job_id=str(created["job_id"]),
        message="attachment failed",
        retryable=True,
        result=payload,
        relay_result=relay_result,
        owner="failure-owner",
    )
    stored = store.get_metadata_job(str(created["job_id"]))

    assert failed["status"] == "failed_retryable"
    assert failed["result_json"] == expected_json
    assert failed["relay_status"] == "failed"
    assert json.loads(str(failed["relay_result"])) == relay_result
    assert stored is not None
    assert stored["result_json"] != expected_json
    assert len(str(stored["result_json"]).encode("utf-8")) <= 64 * 1024
    compacted = json.loads(str(stored["result_json"]))
    assert compacted["_compacted"]["original_bytes"] == len(
        expected_json.encode("utf-8")
    )
    assert compacted["summary"]["worker_status"] == "html_found"
    assert stored["relay_status"] == "failed"
    assert json.loads(str(stored["relay_result"])) == relay_result


def test_stale_metadata_owner_cannot_overwrite_failure_evidence(tmp_path):
    source = tmp_path / "zotero.sqlite"
    source.write_bytes(b"state")
    store = PipelineStateStore(tmp_path / "state.sqlite")
    created = store.enqueue_metadata_job(
        job_type="full_text",
        library_id="LIB1",
        attachment_key="PARENT-FAILURE-FENCE",
        data_dir=tmp_path,
        source_path=source,
        signature=FileSignature.from_path(source),
        status="queued",
        reason="test",
        parent_item_key="PARENT-FAILURE-FENCE",
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

    stale_running = store.mark_metadata_job_failed(
        job_id=job_id,
        message="old failure",
        retryable=True,
        result={"generation": "old"},
        relay_result={"ok": False, "generation": "old"},
        owner="old-owner",
    )
    assert stale_running["status"] == "running"
    assert stale_running["lease_owner"] == "new-owner"
    assert stale_running["result_json"] is None
    assert stale_running["relay_result"] is None

    current = store.mark_metadata_job_failed(
        job_id=job_id,
        message="new failure",
        retryable=True,
        result={"generation": "new"},
        relay_result={"ok": False, "generation": "new"},
        owner="new-owner",
    )
    stale_terminal = store.mark_metadata_job_failed(
        job_id=job_id,
        message="late old failure",
        retryable=False,
        result={"generation": "old-late"},
        relay_result={"ok": True, "generation": "old-late"},
        owner="old-owner",
    )

    assert current["status"] == "failed_retryable"
    assert stale_terminal["status"] == "failed_retryable"
    assert json.loads(str(stale_terminal["result_json"])) == {"generation": "new"}
    assert stale_terminal["relay_status"] == "failed"
    assert json.loads(str(stale_terminal["relay_result"])) == {
        "ok": False,
        "generation": "new",
    }
    with store._connect() as connection:
        events = [
            str(row["event"])
            for row in connection.execute(
                "select event from metadata_job_events where job_id = ? order by event_id",
                (job_id,),
            ).fetchall()
        ]
    assert events.count("stale_completion_discarded") == 2
    assert events.count("failed_retryable") == 1


@pytest.mark.parametrize(
    "relay_result",
    [
        {"ok": "true"},
        {},
        [],
        {"ok": True, "dryRun": "false"},
        {"ok": True, "skipped": "false"},
    ],
    ids=[
        "truthy-ok",
        "missing-ok",
        "non-object",
        "malformed-dry-run",
        "malformed-skip",
    ],
)
def test_metadata_failure_marks_malformed_relay_result_invalid(
    tmp_path,
    relay_result,
):
    source = tmp_path / "zotero.sqlite"
    source.write_bytes(b"state")
    store = PipelineStateStore(tmp_path / "state.sqlite")
    created = store.enqueue_metadata_job(
        job_type="full_text",
        library_id="LIB1",
        attachment_key="PARENT-INVALID-RELAY",
        data_dir=tmp_path,
        source_path=source,
        signature=FileSignature.from_path(source),
        status="queued",
        reason="test",
        parent_item_key="PARENT-INVALID-RELAY",
        queue_key="full-text-v1",
    )
    leased = store.lease_next_metadata_job(
        job_type="full_text",
        owner="failure-owner",
        lease_seconds=60,
    )
    assert leased is not None

    failed = store.mark_metadata_job_failed(
        job_id=str(created["job_id"]),
        message="invalid relay result",
        retryable=True,
        relay_result=relay_result,
        owner="failure-owner",
    )

    assert failed["status"] == "failed_retryable"
    assert failed["relay_status"] == "invalid"


def _completed_html_relay_status(tmp_path, relay_result: object) -> str | None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF")
    store = PipelineStateStore(tmp_path / "state.sqlite")
    created = store.enqueue_html_job(
        library_id="LIB1",
        attachment_key="PDF-RELAY-STATUS",
        data_dir=tmp_path,
        source_path=source,
        signature=FileSignature.from_path(source),
        collection_key="direct_pdf",
        status="queued",
        reason="relay status contract",
    )
    leased = store.lease_next_html_job(owner="relay-owner", lease_seconds=60)
    assert leased is not None

    completed = store.mark_html_job_succeeded(
        job_id=str(created["job_id"]),
        message="relay status contract",
        relay_result=relay_result,
        owner="relay-owner",
    )
    value = completed["relay_status"]
    return str(value) if value is not None else None


@pytest.mark.parametrize(
    "relay_result",
    [
        [],
        {},
        {"ok": "true"},
        {"attachments": []},
        {"attachments": {"en": []}},
        {"attachments": {"en": {}}},
        {"attachments": {"en": {"ok": "true"}}},
        {"attachments": {"en": {"relay": []}}},
        {"attachments": {"en": {"relay": {}}}},
        {"attachments": {"en": {"relay": {"ok": "true"}}}},
        {"attachments": {1: {"ok": True}}},
        {"attachments": {" ": {"ok": True}}},
        {"attachments": {"en": {"ok": True, "skipped": "false"}}},
        {"attachments": {"en": {"ok": True, "required": "false"}}},
        {"attachments": {"en": {"relay": {"ok": True, "dryRun": "false"}}}},
        {"attachments": {"en": {"relay": {"ok": True, "skipped": "false"}}}},
        {"ok": True, "skipped": "false"},
    ],
    ids=[
        "non-object",
        "empty-object",
        "truthy-ok",
        "attachments-not-object",
        "attachment-not-object",
        "attachment-missing-contract",
        "attachment-truthy-ok",
        "nested-relay-not-object",
        "nested-relay-missing-ok",
        "nested-relay-truthy-ok",
        "non-string-attachment-key",
        "empty-attachment-key",
        "attachment-malformed-skipped",
        "attachment-malformed-required",
        "nested-relay-malformed-dry-run",
        "nested-relay-malformed-skipped",
        "outer-malformed-skipped",
    ],
)
def test_html_completion_marks_malformed_relay_result_invalid(
    tmp_path,
    relay_result,
):
    assert _completed_html_relay_status(tmp_path, relay_result) == "invalid"


@pytest.mark.parametrize(
    "relay_result, expected",
    [
        ({"attachments": {}}, "skipped"),
        ({"ok": True}, "succeeded"),
        ({"ok": False}, "failed_optional"),
        ({"attachments": {"en": {"ok": True}}}, "succeeded"),
        ({"attachments": {"en": {"ok": False}}}, "failed_optional"),
        (
            {"attachments": {"en": {"relay": {"ok": True}}}},
            "succeeded",
        ),
        (
            {"attachments": {"en": {"relay": {"ok": False}}}},
            "failed_optional",
        ),
    ],
)
def test_html_completion_preserves_valid_relay_statuses(
    tmp_path,
    relay_result,
    expected: str,
):
    assert _completed_html_relay_status(tmp_path, relay_result) == expected


def test_expired_metadata_lease_cannot_heartbeat_or_complete(tmp_path) -> None:
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
    assert (
        store.lease_next_metadata_job(
            job_type="full_text",
            owner="expired-owner",
            lease_seconds=60,
        )
        is not None
    )
    with store._connect() as connection:
        connection.execute(
            "update metadata_jobs set leased_until = ? where job_id = ?",
            ("2000-01-01T00:00:00+00:00", job_id),
        )

    heartbeat = store.heartbeat_metadata_job(
        job_id=job_id,
        owner="expired-owner",
        lease_seconds=600,
    )
    completion = store.mark_metadata_job_succeeded(
        job_id=job_id,
        message="late completion",
        owner="expired-owner",
    )

    assert heartbeat is False
    assert completion["status"] == "running"
    assert completion["lease_owner"] == "expired-owner"
    assert store.recover_expired_metadata_jobs(job_type="full_text") == 1
    assert store.get_metadata_job(job_id)["status"] == "queued"


def test_expired_html_lease_cannot_heartbeat_or_complete(tmp_path) -> None:
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
    assert (
        store.lease_next_html_job(
            owner="expired-owner",
            lease_seconds=60,
        )
        is not None
    )
    with store._connect() as connection:
        connection.execute(
            "update html_jobs set leased_until = ? where job_id = ?",
            ("2000-01-01T00:00:00+00:00", job_id),
        )

    heartbeat = store.heartbeat_html_job(
        job_id=job_id,
        owner="expired-owner",
        lease_seconds=600,
    )
    completion = store.mark_html_job_succeeded(
        job_id=job_id,
        message="late completion",
        owner="expired-owner",
    )

    assert heartbeat is False
    assert completion["status"] == "running"
    assert completion["lease_owner"] == "expired-owner"
    assert store.recover_expired_html_jobs() == 1
    assert store.get_html_job(job_id)["status"] == "queued"
