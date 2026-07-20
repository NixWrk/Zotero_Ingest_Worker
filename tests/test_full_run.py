from __future__ import annotations

import concurrent.futures
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from zotero_ingest_worker.full_run import (
    FullRunManager,
    FullRunOptions,
    _result_failure_count,
)
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


def test_ingest_run_options_do_not_enqueue_scihub_backlog_when_scihub_drain_is_disabled() -> (
    None
):
    options = FullRunOptions.from_payload(
        {"full_text_drain": True, "scihub_pdf_drain": False}
    )

    assert options.full_text_drain is True
    assert options.scihub_pdf_drain is False
    assert options.scihub_pdf_backlog_intake is False


def test_ingest_run_options_require_exact_boolean_values() -> None:
    for malformed in ("false", "true", 0, 1, [], [True]):
        with pytest.raises(ValueError, match="full_text_drain must be a JSON boolean"):
            FullRunOptions.from_payload({"full_text_drain": malformed})

    assert (
        FullRunOptions.from_payload({"full_text_drain": False}).full_text_drain is False
    )
    assert (
        FullRunOptions.from_payload({"full_text_drain": True}).full_text_drain is True
    )


@pytest.mark.parametrize(
    "field",
    [
        "max_items",
        "queue_limit",
        "drain_limit",
        "poll_seconds",
        "intake_interval_seconds",
        "idle_cycles_to_complete",
    ],
)
@pytest.mark.parametrize("malformed", ["7", True, 7.0, [], {}])
def test_ingest_run_options_require_exact_integer_values(
    field: str,
    malformed: object,
) -> None:
    with pytest.raises(ValueError, match=rf"{field} must be a JSON integer"):
        FullRunOptions.from_payload({field: malformed})


@pytest.mark.parametrize("field", ["max_items", "queue_limit", "limit"])
def test_ingest_run_options_reject_negative_optional_budgets(field: str) -> None:
    with pytest.raises(ValueError, match=rf"{field} must be non-negative"):
        FullRunOptions.from_payload({field: -1})

    options = FullRunOptions.from_payload({field: 0})
    if field == "max_items":
        assert options.max_items == 0
    else:
        assert options.queue_limit == 0


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
    options = FullRunOptions(
        metadata_drain=True, full_text_drain=True, arxiv_html_drain=True
    )

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

    monkeypatch.setattr(
        "zotero_ingest_worker.full_run.ZoteroMetadataProcessor", fake_processor
    )
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
    assert [event["event"] for event in events[:2]] == [
        "stop_requested",
        "drain_full_text",
    ]


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
    assert (
        manager.state.full_runs.get(str(historical["run_id"]))["status"] == "succeeded"
    )

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


def test_ingest_run_options_validate_shadowed_limit_alias() -> None:
    with pytest.raises(ValueError, match="limit must be a JSON integer"):
        FullRunOptions.from_payload({"queue_limit": 9, "limit": "7"})

    options = FullRunOptions.from_payload({"queue_limit": 9, "limit": 9})
    assert options.queue_limit == 9


def test_ingest_full_run_dry_run_is_read_only_one_shot(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    manager = FullRunManager(SimpleNamespace(state_db_path=tmp_path / "state.sqlite"))
    run = manager.state.full_runs.create(options={"mode": "ingest", "dry_run": True})
    run_id = str(run["run_id"])
    manager._active_run_id = run_id
    calls: list[list[str]] = []

    def forbidden_intake(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("dry-run must not enqueue backlog work")

    def fake_actions(*_args: Any, **_kwargs: Any) -> list[str]:
        return ["full_text"]

    def fake_drain(
        *,
        run_id: str,
        actions: list[str],
        options: FullRunOptions,
    ) -> dict[str, dict[str, Any]]:
        del run_id
        assert options.dry_run is True
        calls.append(actions)
        return {"full_text": {"ok": True, "dry_run": True, "would_process": 2}}

    monkeypatch.setattr(manager, "_run_intake", forbidden_intake)
    monkeypatch.setattr(manager, "_ready_actions", fake_actions)
    monkeypatch.setattr(manager, "_drain_parallel_actions", fake_drain)
    manager._run(run_id, FullRunOptions(dry_run=True))

    completed = manager.state.full_runs.get(run_id)
    events = manager.state.full_runs.events(run_id, limit=20)
    assert calls == [["full_text"]]
    assert completed["status"] == "succeeded"
    assert completed["phase"] == "dry_run_complete"
    assert completed["finished_at"] is not None
    assert any(event["event"] == "dry_run_complete" for event in events)


def test_full_run_claim_reuses_fresh_active_run_across_store_instances(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.sqlite"
    first_store = OcrStateStore(state_path)
    second_store = OcrStateStore(state_path)

    first = first_store.create_full_run(
        options={"mode": "ingest"},
        stale_after_seconds=300,
    )
    second = second_store.create_full_run(
        options={"mode": "ingest"},
        stale_after_seconds=300,
    )

    assert first["created"] is True
    assert second["created"] is False
    assert second["run_id"] == first["run_id"]
    assert first_store.running_full_run()["run_id"] == first["run_id"]


def test_full_run_claim_is_atomic_under_concurrent_store_calls(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"
    stores = [OcrStateStore(state_path), OcrStateStore(state_path)]
    barrier = threading.Barrier(2)

    def claim(store: OcrStateStore) -> dict[str, Any]:
        barrier.wait(timeout=5)
        return store.create_full_run(
            options={"mode": "ingest"},
            stale_after_seconds=300,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(claim, store) for store in stores]
        results = [future.result(timeout=10) for future in futures]

    assert sorted(result["created"] for result in results) == [False, True]
    assert len({str(result["run_id"]) for result in results}) == 1
    with sqlite3.connect(state_path) as connection:
        active_count = connection.execute(
            """
            select count(*)
            from full_runs
            where status in ('running', 'stopping')
              and finished_at is null
            """
        ).fetchone()[0]
    assert active_count == 1


def test_full_run_claim_replaces_only_expired_heartbeat(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"
    store = OcrStateStore(state_path)
    first = store.create_full_run(
        options={"mode": "ingest"},
        stale_after_seconds=300,
    )
    expired = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            "update full_runs set heartbeat_at = ?, updated_at = ? where run_id = ?",
            (expired, expired, first["run_id"]),
        )
        connection.commit()

    replacement = store.create_full_run(
        options={"mode": "ingest"},
        stale_after_seconds=300,
    )
    interrupted = store.get_full_run(str(first["run_id"]))

    assert replacement["created"] is True
    assert replacement["run_id"] != first["run_id"]
    assert interrupted["status"] == "interrupted"
    assert interrupted["phase"] == "interrupted"
    assert interrupted["finished_at"] is not None
    events = store.list_full_run_events(str(first["run_id"]), limit=20)
    assert events[0]["event"] == "interrupted"


def test_full_run_heartbeat_is_active_only_until_terminal(tmp_path: Path) -> None:
    store = OcrStateStore(tmp_path / "state.sqlite")
    run = store.create_full_run(
        options={"mode": "ingest"},
        stale_after_seconds=300,
    )

    assert store.heartbeat_full_run(str(run["run_id"])) is True
    store.update_full_run(
        run_id=str(run["run_id"]),
        status="succeeded",
        phase="complete",
        finished=True,
    )
    assert store.heartbeat_full_run(str(run["run_id"])) is False


def test_full_run_events_reject_unbounded_limits(tmp_path: Path) -> None:
    store = OcrStateStore(tmp_path / "state.sqlite")
    run = store.create_full_run(options={"mode": "ingest"})

    with pytest.raises(ValueError, match="event limit"):
        store.list_full_run_events(str(run["run_id"]), limit=-1)
    with pytest.raises(ValueError, match="event limit"):
        store.list_full_run_events(str(run["run_id"]), limit=1001)
    assert store.list_full_run_events(str(run["run_id"]), limit=0) == []


def test_full_run_start_returns_run_not_nested_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = FullRunManager(SimpleNamespace(state_db_path=tmp_path / "state.sqlite"))
    monkeypatch.setattr(manager, "_run", lambda *_args: None)

    result = manager.start({"dry_run": True})
    assert manager._thread is not None
    manager._thread.join(timeout=2)

    assert result["started"] is True
    assert result["run"]["run_id"] == result["run_id"]
    assert "metadata_queue" not in result["run"]


def test_full_run_claim_preserves_freshest_heartbeat_and_interrupts_duplicates(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.sqlite"
    store = OcrStateStore(state_path)
    first = store.create_full_run(options={"mode": "ingest"})
    now = datetime.now(UTC)
    first_heartbeat = (now - timedelta(seconds=30)).isoformat()
    duplicate_heartbeat = now.isoformat()
    duplicate_run_id = "full_duplicate"
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            "update full_runs set heartbeat_at = ?, updated_at = ? where run_id = ?",
            (first_heartbeat, first_heartbeat, first["run_id"]),
        )
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
            values (?, 'running', 'running', 'ingest', '{}', 0, ?, ?, ?)
            """,
            (
                duplicate_run_id,
                (now - timedelta(days=1)).isoformat(),
                duplicate_heartbeat,
                duplicate_heartbeat,
            ),
        )
        connection.commit()

    claimed = store.create_full_run(
        options={"mode": "ingest"},
        stale_after_seconds=300,
    )

    assert claimed["created"] is False
    assert claimed["run_id"] == duplicate_run_id
    assert store.get_full_run(str(first["run_id"]))["status"] == "interrupted"
    assert store.get_full_run(duplicate_run_id)["status"] == "running"
    assert store.running_full_run()["run_id"] == duplicate_run_id


@pytest.mark.parametrize(
    "heartbeat", [None, "not-an-iso-timestamp", "2026-07-20T01:00:00"]
)
def test_full_run_claim_treats_missing_malformed_or_naive_heartbeat_as_stale(
    tmp_path: Path,
    heartbeat: str | None,
) -> None:
    state_path = tmp_path / "state.sqlite"
    store = OcrStateStore(state_path)
    original = store.create_full_run(options={"mode": "ingest"})
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            "update full_runs set heartbeat_at = ? where run_id = ?",
            (heartbeat, original["run_id"]),
        )
        connection.commit()

    replacement = store.create_full_run(
        options={"mode": "ingest"},
        stale_after_seconds=300,
    )

    assert replacement["created"] is True
    assert replacement["run_id"] != original["run_id"]
    assert store.get_full_run(str(original["run_id"]))["status"] == "interrupted"


@pytest.mark.parametrize("stale_after_seconds", [True, 0, -1, 1.5, "300", None])
def test_full_run_claim_rejects_invalid_stale_after_seconds(
    tmp_path: Path,
    stale_after_seconds: object,
) -> None:
    store = OcrStateStore(tmp_path / "state.sqlite")

    with pytest.raises(ValueError, match="stale_after_seconds"):
        store.create_full_run(
            options={"mode": "ingest"},
            stale_after_seconds=stale_after_seconds,  # type: ignore[arg-type]
        )


def test_full_run_claim_rolls_back_stale_interrupt_when_create_fails(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.sqlite"
    store = OcrStateStore(state_path)
    original = store.create_full_run(options={"mode": "ingest"})
    expired = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            "update full_runs set heartbeat_at = ? where run_id = ?",
            (expired, original["run_id"]),
        )
        connection.commit()
    circular_options: dict[str, Any] = {"mode": "ingest"}
    circular_options["self"] = circular_options

    with pytest.raises(ValueError, match="Circular reference"):
        store.create_full_run(
            options=circular_options,
            stale_after_seconds=300,
        )

    preserved = store.get_full_run(str(original["run_id"]))
    assert preserved["status"] == "running"
    assert preserved["finished_at"] is None
    assert all(
        event["event"] != "interrupted"
        for event in store.list_full_run_events(str(original["run_id"]), limit=20)
    )


@pytest.mark.parametrize("event_limit", [True, 1.5, "1", None])
def test_full_run_events_reject_non_integer_limits(
    tmp_path: Path,
    event_limit: object,
) -> None:
    store = OcrStateStore(tmp_path / "state.sqlite")
    run = store.create_full_run(options={"mode": "ingest"})

    with pytest.raises(ValueError, match="event limit"):
        store.list_full_run_events(
            str(run["run_id"]),
            limit=event_limit,  # type: ignore[arg-type]
        )


def test_second_manager_adopts_fresh_run_without_starting_thread(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.sqlite"
    first = FullRunManager(SimpleNamespace(state_db_path=state_path))
    existing = first.state.full_runs.create(options={"mode": "ingest"})
    second = FullRunManager(SimpleNamespace(state_db_path=state_path))

    result = second.start({})

    assert result["started"] is False
    assert result["already_running"] is True
    assert result["run_id"] == existing["run_id"]
    assert result["run"]["run_id"] == existing["run_id"]
    assert "created" not in result["run"]
    assert second._thread is None
    assert second._active_run_id is None
    assert second.state.full_runs.get(str(existing["run_id"]))["status"] == "running"


def test_heartbeat_loop_sets_only_the_owned_run_stop_event() -> None:
    manager = object.__new__(FullRunManager)

    class InactiveFullRuns:
        @staticmethod
        def heartbeat(_run_id: str) -> bool:
            return False

    manager.state = SimpleNamespace(full_runs=InactiveFullRuns())
    heartbeat_stop = threading.Event()
    old_run_stop = threading.Event()
    unrelated_run_stop = threading.Event()

    manager._heartbeat_loop("old-run", heartbeat_stop, old_run_stop)

    assert old_run_stop.is_set() is True
    assert unrelated_run_stop.is_set() is False


def test_heartbeat_loop_fails_closed_when_ownership_cannot_be_refreshed() -> None:
    manager = object.__new__(FullRunManager)
    heartbeat_stop = threading.Event()

    class FailingFullRuns:
        @staticmethod
        def heartbeat(_run_id: str) -> bool:
            heartbeat_stop.set()
            raise RuntimeError("state unavailable")

    manager.state = SimpleNamespace(full_runs=FailingFullRuns())
    run_stop = threading.Event()

    manager._heartbeat_loop("owned-run", heartbeat_stop, run_stop)

    assert run_stop.is_set() is True


def test_full_run_base_exception_is_terminalized_before_propagation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class AbortRun(BaseException):
        pass

    manager = FullRunManager(SimpleNamespace(state_db_path=tmp_path / "state.sqlite"))
    run = manager.state.full_runs.create(options={"mode": "ingest", "dry_run": True})
    run_id = str(run["run_id"])
    manager._active_run_id = run_id

    def abort(*_args: Any, **_kwargs: Any) -> None:
        raise AbortRun("cancelled")

    monkeypatch.setattr(manager, "_run_dry_run", abort)

    with pytest.raises(AbortRun, match="cancelled"):
        manager._run(run_id, FullRunOptions(dry_run=True))

    stored = manager.state.full_runs.get(run_id)
    assert stored["status"] == "failed"
    assert stored["phase"] == "failed"
    assert stored["finished_at"] is not None
    assert manager._active_run_id is None


def test_full_run_claim_interrupts_all_stale_duplicates_before_new_run(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.sqlite"
    store = OcrStateStore(state_path)
    first = store.create_full_run(options={"mode": "ingest"})
    expired = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    duplicate_run_id = "full_stale_duplicate"
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            "update full_runs set heartbeat_at = ? where run_id = ?",
            (expired, first["run_id"]),
        )
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
            values (?, 'running', 'running', 'ingest', '{}', 0, ?, ?, ?)
            """,
            (duplicate_run_id, expired, expired, expired),
        )
        connection.commit()

    replacement = store.create_full_run(
        options={"mode": "ingest"},
        stale_after_seconds=300,
    )

    assert replacement["created"] is True
    assert store.get_full_run(str(first["run_id"]))["status"] == "interrupted"
    assert store.get_full_run(duplicate_run_id)["status"] == "interrupted"
    assert store.running_full_run()["run_id"] == replacement["run_id"]
    with sqlite3.connect(state_path) as connection:
        active_count = connection.execute(
            """
            select count(*)
            from full_runs
            where status in ('running', 'stopping')
              and finished_at is null
            """
        ).fetchone()[0]
    assert active_count == 1


@pytest.mark.parametrize("event_limit", [-1, 1001, True, 1.5, "10", None])
def test_full_run_manager_status_rejects_invalid_event_limit(
    tmp_path: Path,
    event_limit: object,
) -> None:
    manager = FullRunManager(SimpleNamespace(state_db_path=tmp_path / "state.sqlite"))

    with pytest.raises(ValueError, match="event_limit"):
        manager.status(event_limit=event_limit)  # type: ignore[arg-type]


def test_full_run_thread_start_failure_is_terminalized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = FullRunManager(SimpleNamespace(state_db_path=tmp_path / "state.sqlite"))

    def fail_start(_thread: threading.Thread) -> None:
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(threading.Thread, "start", fail_start)

    with pytest.raises(RuntimeError, match="thread unavailable"):
        manager.start({})

    stored = manager.state.full_runs.latest()
    assert stored["status"] == "failed"
    assert stored["phase"] == "thread_start_failed"
    assert stored["finished_at"] is not None
    assert stored["last_error"] == "thread unavailable"
    assert manager._thread is None
    assert manager._active_run_id is None


def test_full_run_heartbeat_runs_while_dry_run_handler_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = FullRunManager(SimpleNamespace(state_db_path=tmp_path / "state.sqlite"))
    run = manager.state.full_runs.create(options={"mode": "ingest", "dry_run": True})
    run_id = str(run["run_id"])
    manager._active_run_id = run_id
    handler_entered = threading.Event()
    handler_release = threading.Event()
    heartbeat_seen = threading.Event()
    original_heartbeat = manager.state.heartbeat_full_run
    original_dry_run = manager._run_dry_run

    def observed_heartbeat(target_run_id: str) -> bool:
        heartbeat_seen.set()
        return original_heartbeat(target_run_id)

    def blocked_dry_run(target_run_id: str, options: FullRunOptions) -> None:
        handler_entered.set()
        if not handler_release.wait(timeout=5):
            raise TimeoutError("test did not release the dry-run handler")
        original_dry_run(target_run_id, options)

    monkeypatch.setattr(manager.state, "heartbeat_full_run", observed_heartbeat)
    monkeypatch.setattr(manager, "_run_dry_run", blocked_dry_run)
    worker = threading.Thread(
        target=manager._run,
        args=(run_id, FullRunOptions(dry_run=True)),
        daemon=True,
    )
    worker.start()
    try:
        assert handler_entered.wait(timeout=5)
        assert heartbeat_seen.wait(timeout=5)
        assert manager.state.full_runs.get(run_id)["status"] == "running"
    finally:
        handler_release.set()
        worker.join(timeout=5)

    assert worker.is_alive() is False
    stored = manager.state.full_runs.get(run_id)
    assert stored["status"] == "succeeded"
    assert stored["phase"] == "dry_run_complete"
    assert stored["finished_at"] is not None


@pytest.mark.parametrize(
    "field, value",
    [
        ("max_items", 1_000_001),
        ("queue_limit", 1_000_001),
        ("limit", 1_000_001),
        ("drain_limit", 0),
        ("drain_limit", -1),
        ("drain_limit", 50_001),
        ("poll_seconds", 4),
        ("poll_seconds", 86_401),
        ("intake_interval_seconds", 29),
        ("intake_interval_seconds", 86_401),
        ("idle_cycles_to_complete", 0),
        ("idle_cycles_to_complete", 1_000_001),
    ],
)
def test_ingest_run_options_reject_out_of_range_operation_controls(
    field: str,
    value: int,
) -> None:
    with pytest.raises(ValueError, match=field):
        FullRunOptions.from_payload({field: value})


def test_ingest_run_options_accept_documented_operation_boundaries() -> None:
    options = FullRunOptions.from_payload(
        {
            "max_items": 1_000_000,
            "queue_limit": 1_000_000,
            "drain_limit": 50_000,
            "poll_seconds": 5,
            "intake_interval_seconds": 30,
            "idle_cycles_to_complete": 1_000_000,
        }
    )

    assert options.max_items == 1_000_000
    assert options.queue_limit == 1_000_000
    assert options.drain_limit == 50_000
    assert options.poll_seconds == 5
    assert options.intake_interval_seconds == 30
    assert options.idle_cycles_to_complete == 1_000_000

    upper_time = FullRunOptions.from_payload(
        {
            "poll_seconds": 86_400,
            "intake_interval_seconds": 86_400,
        }
    )
    assert upper_time.poll_seconds == 86_400
    assert upper_time.intake_interval_seconds == 86_400


@pytest.mark.parametrize(
    "payload, expected_max_items, expected_queue_limit",
    [
        ({"max_items": 0}, 0, None),
        ({"queue_limit": 0}, None, 0),
        ({"limit": 0}, None, 0),
        ({"queue_limit": 0, "limit": 0}, None, 0),
    ],
)
def test_ingest_run_options_preserve_explicit_zero_budgets(
    payload: dict[str, int],
    expected_max_items: int | None,
    expected_queue_limit: int | None,
) -> None:
    options = FullRunOptions.from_payload(payload)

    assert options.max_items == expected_max_items
    assert options.queue_limit == expected_queue_limit


def test_ingest_run_options_reject_conflicting_queue_limit_aliases() -> None:
    with pytest.raises(ValueError, match="queue_limit and limit"):
        FullRunOptions.from_payload({"queue_limit": 1, "limit": 2})


def test_ingest_run_options_accept_equivalent_queue_limit_aliases() -> None:
    options = FullRunOptions.from_payload({"queue_limit": 2, "limit": 2})

    assert options.queue_limit == 2
