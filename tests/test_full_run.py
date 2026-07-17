from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

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


def test_ingest_run_ready_actions_skip_stages_already_running() -> None:
    options = FullRunOptions(metadata_drain=True, full_text_drain=True, arxiv_html_drain=True)

    actions = FullRunManager._ready_actions(
        options,
        metadata_queue={"queued": 1, "running": 1},
        full_text_queue={"queued": 1, "running": 0},
        arxiv_html_queue={"queued": 1, "running": 0},
    )

    assert actions == ["full_text", "arxiv_html"]


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


def test_ingest_run_drains_ready_actions_together(monkeypatch) -> None:
    manager = object.__new__(FullRunManager)
    manager.config = object()
    calls: list[str] = []

    def fake_processor(config: object) -> object:
        return object()

    def fake_drain_action(
        *,
        run_id: str,
        action: str,
        options: FullRunOptions,
        metadata: object,
    ) -> dict[str, Any]:
        calls.append(action)
        return {"processed": 1, "failed": 0}

    monkeypatch.setattr("zotero_ingest_worker.full_run.ZoteroMetadataProcessor", fake_processor)
    manager._drain_action = fake_drain_action

    results = manager._drain_parallel_actions(
        run_id="run-1",
        actions=["metadata", "full_text"],
        options=FullRunOptions(),
    )

    assert set(results) == {"metadata", "full_text"}
    assert sorted(calls) == ["full_text", "metadata"]


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


def test_ingest_full_run_stop_is_idempotent_and_terminal_safe(tmp_path: Path) -> None:
    store = OcrStateStore(tmp_path / "state.sqlite")

    run = store.create_full_run(options={"mode": "ingest"})
    first = store.request_full_run_stop(str(run["run_id"]))
    repeated = store.request_full_run_stop(str(run["run_id"]))

    assert first["status"] == "stopping"
    assert repeated["status"] == "stopping"
    events = store.list_full_run_events(str(run["run_id"]), limit=20)
    assert [event["event"] for event in events].count("stop_requested") == 1

    completed = store.update_full_run(
        run_id=str(run["run_id"]),
        status="stopped",
        phase="stopped",
        finished=True,
    )
    terminal_attempt = store.request_full_run_stop(str(run["run_id"]))
    terminal_events = store.list_full_run_events(str(run["run_id"]), limit=20)

    assert completed["status"] == "stopped"
    assert terminal_attempt["status"] == "stopped"
    assert [event["event"] for event in terminal_events].count("stop_requested") == 1

    untouched = store.create_full_run(options={"mode": "ingest"})
    store.update_full_run(
        run_id=str(untouched["run_id"]),
        status="succeeded",
        phase="complete",
        finished=True,
    )
    untouched_stop = store.request_full_run_stop(str(untouched["run_id"]))
    untouched_events = store.list_full_run_events(str(untouched["run_id"]), limit=20)

    assert untouched_stop["status"] == "succeeded"
    assert untouched_stop["stop_requested"] == 0
    assert all(event["event"] != "stop_requested" for event in untouched_events)


def test_ingest_manager_does_not_stop_active_run_for_historical_target(
    tmp_path: Path,
) -> None:
    manager = FullRunManager(SimpleNamespace(state_db_path=tmp_path / "state.sqlite"))
    historical = manager.state.full_runs.create(options={"mode": "ingest"})
    manager.state.full_runs.update(
        run_id=str(historical["run_id"]),
        status="succeeded",
        phase="complete",
        finished=True,
    )
    active = manager.state.full_runs.create(options={"mode": "ingest"})
    manager._active_run_id = str(active["run_id"])
    manager._stop_event.clear()

    ignored = manager.stop(str(historical["run_id"]))

    assert ignored["stop_requested"] is False
    assert manager._stop_event.is_set() is False
    assert manager.state.full_runs.get(str(active["run_id"]))["status"] == "running"
    assert manager.state.full_runs.get(str(historical["run_id"]))["status"] == "succeeded"

    accepted = manager.stop(str(active["run_id"]))

    assert accepted["stop_requested"] is True
    assert manager._stop_event.is_set() is True


def test_late_ingest_run_update_cannot_rewrite_terminal_state(tmp_path: Path) -> None:
    store = OcrStateStore(tmp_path / "state.sqlite")

    run = store.create_full_run(options={"mode": "ingest"})
    terminal = store.update_full_run(
        run_id=str(run["run_id"]),
        status="succeeded",
        phase="complete",
        finished=True,
        event="succeeded",
        message="complete",
    )
    events_before = store.list_full_run_events(str(run["run_id"]), limit=20)

    late = store.update_full_run(
        run_id=str(run["run_id"]),
        status="failed",
        phase="late_background_failure",
        last_error="late",
        finished=True,
        event="late_background_failure",
        message="late",
    )
    events_after = store.list_full_run_events(str(run["run_id"]), limit=20)

    assert terminal["status"] == "succeeded"
    assert late["status"] == "succeeded"
    assert late["phase"] == "complete"
    assert late["last_error"] is None
    assert late["updated_at"] == terminal["updated_at"]
    assert events_after == events_before
