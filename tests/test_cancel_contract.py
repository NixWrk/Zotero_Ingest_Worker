from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from zotero_ingest_worker.state import FileSignature, PipelineStateStore


Job = dict[str, Any]


def _event_count(
    store: PipelineStateStore,
    *,
    table: str,
    job_id: str,
) -> int:
    assert table in {
        "html_job_events",
        "metadata_job_events",
        "ocr_job_events",
    }
    with store._connect() as connection:
        row = connection.execute(
            f"select count(*) from {table} where job_id = ?",
            (job_id,),
        ).fetchone()
    return int(row[0])


def _assert_cancel_contract(
    store: PipelineStateStore,
    *,
    event_table: str,
    create_job: Callable[[str], Job],
    lease_job: Callable[[], Job | None],
    cancel_job: Callable[[str], Job],
    fail_job: Callable[[str], Job],
) -> None:
    queued = create_job("queued")
    queued_id = str(queued["job_id"])
    queued_events = _event_count(store, table=event_table, job_id=queued_id)

    cancelled = cancel_job(queued_id)

    assert cancelled["status"] == "cancelled"
    assert _event_count(store, table=event_table, job_id=queued_id) == queued_events + 1

    cancelled_events = _event_count(store, table=event_table, job_id=queued_id)
    assert cancel_job(queued_id)["status"] == "cancelled"
    assert _event_count(store, table=event_table, job_id=queued_id) == cancelled_events
    assert cancel_job("missing-job") == {}

    running = create_job("running")
    running_id = str(running["job_id"])
    leased = lease_job()
    assert leased is not None
    assert leased["job_id"] == running_id
    running_events = _event_count(store, table=event_table, job_id=running_id)

    unchanged_running = cancel_job(running_id)

    assert unchanged_running["status"] == "running"
    assert _event_count(store, table=event_table, job_id=running_id) == running_events

    terminal = create_job("terminal")
    terminal_id = str(terminal["job_id"])
    failed = fail_job(terminal_id)
    assert failed["status"] == "failed_final"
    failed_events = _event_count(store, table=event_table, job_id=terminal_id)

    unchanged_terminal = cancel_job(terminal_id)

    assert unchanged_terminal["status"] == "failed_final"
    assert _event_count(store, table=event_table, job_id=terminal_id) == failed_events


def test_html_cancel_event_requires_applied_transition(tmp_path: Path) -> None:
    store = PipelineStateStore(tmp_path / "state.sqlite")

    def create_job(label: str) -> Job:
        source = tmp_path / f"{label}.html"
        source.write_text("<html>test</html>", encoding="utf-8")
        return store.enqueue_html_job(
            library_id="LIB",
            attachment_key=f"HTML-{label}",
            data_dir=tmp_path,
            source_path=source,
            signature=FileSignature.from_path(source),
            collection_key="direct_pdf",
            status="queued",
            reason="cancel contract",
        )

    _assert_cancel_contract(
        store,
        event_table="html_job_events",
        create_job=create_job,
        lease_job=lambda: store.lease_next_html_job(owner="worker", lease_seconds=60),
        cancel_job=store.cancel_html_job,
        fail_job=lambda job_id: store.mark_html_job_failed(
            job_id=job_id,
            message="terminal",
            retryable=False,
        ),
    )


def test_metadata_cancel_event_requires_applied_transition(tmp_path: Path) -> None:
    store = PipelineStateStore(tmp_path / "state.sqlite")

    def create_job(label: str) -> Job:
        source = tmp_path / f"{label}.sqlite"
        source.write_bytes(b"metadata")
        return store.enqueue_metadata_job(
            job_type="full_text",
            library_id="LIB",
            attachment_key=f"META-{label}",
            data_dir=tmp_path,
            source_path=source,
            signature=FileSignature.from_path(source),
            status="queued",
            reason="cancel contract",
        )

    _assert_cancel_contract(
        store,
        event_table="metadata_job_events",
        create_job=create_job,
        lease_job=lambda: store.lease_next_metadata_job(
            job_type="full_text",
            owner="worker",
            lease_seconds=60,
        ),
        cancel_job=store.cancel_metadata_job,
        fail_job=lambda job_id: store.mark_metadata_job_failed(
            job_id=job_id,
            message="terminal",
            retryable=False,
        ),
    )


def test_ocr_cancel_event_requires_applied_transition(tmp_path: Path) -> None:
    store = PipelineStateStore(tmp_path / "state.sqlite")

    def create_job(label: str) -> Job:
        source = tmp_path / f"{label}.pdf"
        source.write_bytes(b"%PDF-1.4 cancel contract")
        return store.enqueue_job(
            library_id="LIB",
            attachment_key=f"OCR-{label}",
            data_dir=tmp_path,
            source_path=source,
            signature=FileSignature.from_path(source),
            status="queued",
            reason="cancel contract",
        )

    _assert_cancel_contract(
        store,
        event_table="ocr_job_events",
        create_job=create_job,
        lease_job=lambda: store.lease_next_job(owner="worker", lease_seconds=60),
        cancel_job=store.cancel_job,
        fail_job=lambda job_id: store.mark_job_failed(
            job_id=job_id,
            message="terminal",
            retryable=False,
        ),
    )
