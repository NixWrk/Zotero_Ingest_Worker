from __future__ import annotations

from pathlib import Path

from zotero_ingest_worker.full_run import FullRunManager, FullRunOptions, _result_failure_count
from zotero_ingest_worker.state import OcrStateStore


def test_ingest_run_options_default_to_metadata_and_files() -> None:
    options = FullRunOptions.from_payload({})

    assert options.drain_limit == 1
    assert options.metadata_backlog_intake is True
    assert options.full_text_backlog_intake is True
    assert options.arxiv_html_backlog_intake is True
    assert options.full_text_drain is True
    assert options.researchgate_pdf_drain is True
    assert options.scihub_pdf_backlog_intake is True
    assert options.scihub_pdf_drain is True


def test_ingest_run_options_enable_researchgate_pdf_with_full_text_drain() -> None:
    options = FullRunOptions.from_payload({"full_text_drain": True})

    assert options.full_text_drain is True
    assert options.researchgate_pdf_drain is True
    assert options.scihub_pdf_backlog_intake is True
    assert options.scihub_pdf_drain is True


def test_ingest_run_options_do_not_enqueue_scihub_backlog_when_scihub_drain_is_disabled() -> None:
    options = FullRunOptions.from_payload({"full_text_drain": True, "scihub_pdf_drain": False})

    assert options.full_text_drain is True
    assert options.scihub_pdf_drain is False
    assert options.scihub_pdf_backlog_intake is False


def test_ingest_run_next_action_prioritizes_metadata_then_file_discovery() -> None:
    options = FullRunOptions(metadata_drain=True, full_text_drain=True)

    metadata_action = FullRunManager._next_action(
        options,
        metadata_queue={"queued": 1},
        full_text_queue={"queued": 1},
    )
    full_text_action = FullRunManager._next_action(
        options,
        metadata_queue={"queued": 0},
        full_text_queue={"queued": 1},
    )

    assert metadata_action == "metadata"
    assert full_text_action == "full_text"


def test_ingest_run_next_action_covers_file_fallbacks() -> None:
    options = FullRunOptions(
        metadata_drain=True,
        arxiv_html_drain=True,
        full_text_drain=True,
        researchgate_pdf_drain=True,
    )

    researchgate_action = FullRunManager._next_action(
        options,
        metadata_queue={"queued": 0},
        arxiv_html_queue={"queued": 1},
        full_text_queue={"queued": 0},
        researchgate_pdf_queue={"queued": 1},
    )
    arxiv_action = FullRunManager._next_action(
        options,
        metadata_queue={"queued": 0},
        arxiv_html_queue={"queued": 1},
        full_text_queue={"queued": 0},
        researchgate_pdf_queue={"queued": 0},
    )
    scihub_backlog_action = FullRunManager._next_action(
        options,
        metadata_queue={"queued": 0},
        arxiv_html_queue={"queued": 0},
        full_text_queue={"queued": 0},
        researchgate_pdf_queue={"queued": 0},
        scihub_pdf_queue={"queued": 0},
        scihub_pdf_backlog_pending=True,
    )
    scihub_action = FullRunManager._next_action(
        options,
        metadata_queue={"queued": 0},
        arxiv_html_queue={"queued": 0},
        full_text_queue={"queued": 0},
        researchgate_pdf_queue={"queued": 0},
        scihub_pdf_queue={"queued": 1},
        scihub_pdf_backlog_pending=False,
    )

    assert researchgate_action == "researchgate_pdf"
    assert arxiv_action == "arxiv_html"
    assert scihub_backlog_action == "scihub_pdf_backlog"
    assert scihub_action == "scihub_pdf"


def test_ingest_run_counts_batch_failures_for_completion_status() -> None:
    assert _result_failure_count({"processed": 1, "failed": 1}) == 1
    assert _result_failure_count({"processed": 1, "failed": 0}) == 0


def test_ingest_run_state_records_status_and_events(tmp_path: Path) -> None:
    store = OcrStateStore(tmp_path / "state.sqlite")

    run = store.create_full_run(options={"mode": "ingest"})
    updated = store.update_full_run(
        run_id=run["run_id"],
        phase="draining_full_text",
        event="drain_full_text",
        message="Draining full text.",
        metadata={"queued": 1},
    )
    stopped = store.request_full_run_stop(run["run_id"])
    events = store.list_full_run_events(run["run_id"], limit=10)

    assert updated["phase"] == "draining_full_text"
    assert stopped["stop_requested"] == 1
    assert [event["event"] for event in events[:2]] == ["stop_requested", "drain_full_text"]
