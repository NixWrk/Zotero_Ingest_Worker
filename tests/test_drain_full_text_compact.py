from __future__ import annotations

import json
import io
import threading
import time
import pytest

from dataclasses import dataclass, replace

import scripts.drains.full_text_compact as drain_full_text_compact
from scripts.drains.full_text_compact import compact_batch, summarize_job


@dataclass(frozen=True)
class _FakeWorkerConfig:
    metadata_drain_max_workers: int = 7


def test_compact_full_text_summary_uses_worker_status_and_accepted_html() -> None:
    payload = {
        "status": "html_found",
        "worker_status": "existing_pdf_html_queued",
        "html_downloads": [
            {
                "ok": True,
                "article_verdict": {"ok": False, "reason": "weak_landing"},
            }
        ],
        "existing_pdf_enqueue": {
            "html_enqueue": {
                "classification": "queued",
                "job": {"job_id": "html_pdf_1"},
            }
        },
    }
    job = {
        "job_id": "meta_1",
        "status": "succeeded",
        "result_json": json.dumps(payload),
    }

    summary = summarize_job(job)
    batch = compact_batch({"ok": True, "processed": 1, "failed": 0, "results": [job]})

    assert summary["full_text_status"] == "existing_pdf_html_queued"
    assert summary["html_found"] is False
    assert summary["html_rejected"] is True
    assert summary["existing_pdf_html_job_id"] == "html_pdf_1"
    assert batch["html_found"] == 0
    assert batch["html_rejected"] == 1
    assert batch["existing_pdf_html_queued"] == 1


def test_compact_full_text_summary_reports_combined_html_pdf_attachment() -> None:
    payload = {
        "worker_status": "html_and_pdf_found",
        "html_downloads": [
            {
                "ok": True,
                "kind": "html",
                "output_path": "/tmp/article.html",
                "article": {
                    "ok": True,
                    "text_chars": 12000,
                    "markers": ["article_tag"],
                },
            },
        ],
        "pdf_downloads": [
            {"ok": True, "output_path": "/tmp/article.pdf"},
        ],
        "relay_attachment": {
            "kind": "html",
            "attached_kinds": ["html", "pdf"],
            "relay": {"newAttachmentKey": "HTML1234"},
            "pdf_attachment": {
                "kind": "pdf",
                "relay": {"newAttachmentKey": "PDF1234"},
            },
        },
    }
    job = {
        "job_id": "meta_2",
        "status": "succeeded",
        "result_json": json.dumps(payload),
    }

    summary = summarize_job(job)
    batch = compact_batch({"ok": True, "processed": 1, "failed": 0, "results": [job]})

    assert summary["full_text_status"] == "html_and_pdf_found"
    assert summary["attached_kinds"] == ["html", "pdf"]
    assert summary["new_attachment_key"] == "HTML1234"
    assert summary["pdf_new_attachment_key"] == "PDF1234"
    assert summary["html_found"] is True
    assert summary["pdf_found"] is True
    assert batch["html_found"] == 1
    assert batch["pdf_found"] == 1


def test_compact_full_text_summary_counts_browser_pdf_fallbacks() -> None:
    payload = {
        "status": "unresolved",
        "existing_full_text_inventory": {"has_pdf": False},
        "discovery": {
            "locations": [
                {
                    "source": "semantic_scholar",
                    "url": "https://www.researchgate.net/publication/123_example",
                    "kind": "landing",
                }
            ]
        },
        "pdf_downloads": [],
    }
    job = {
        "job_id": "meta_3",
        "status": "succeeded",
        "result_json": json.dumps(payload),
    }

    summary = summarize_job(job)
    batch = compact_batch({"ok": True, "processed": 1, "failed": 0, "results": [job]})

    assert summary["full_text_status"] == "browser_pdf_fallback_available"
    assert summary["browser_pdf_fallbacks"] == 1
    assert batch["browser_pdf_fallbacks"] == 1


def test_compact_scihub_pdf_summary_reports_attached_pdf() -> None:
    payload = {
        "ok": True,
        "status": "attached",
        "download": {
            "ok": True,
            "status": "downloaded",
            "doi": "10.123/example",
            "scihub_url": "https://example.test/10.123/example",
            "pdf_url": "https://example.test/example.pdf",
            "output_path": "/tmp/example.pdf",
        },
        "attach": {
            "ok": True,
            "relay": {"newAttachmentKey": "PDF1234"},
        },
    }
    job = {
        "job_id": "scihub_1",
        "parent_item_key": "PARENT1",
        "attachment_key": "PARENT1",
        "status": "succeeded",
        "result_json": json.dumps(payload),
    }

    batch = compact_batch(
        {"ok": True, "processed": 1, "failed": 0, "results": [job]},
        job_type="scihub_pdf",
    )

    assert batch["job_type"] == "scihub_pdf"
    assert batch["pdf_found"] == 1
    assert batch["scihub_attached"] == 1
    assert batch["scihub_downloaded"] == 1
    assert batch["already_has_pdf"] == 0
    assert batch["jobs"][0]["new_attachment_key"] == "PDF1234"
    assert batch["jobs"][0]["doi"] == "10.123/example"


def test_compact_scihub_summary_rejects_truthy_success_flags() -> None:
    payload = {
        "ok": "true",
        "status": "attached",
        "download": {
            "ok": "true",
            "skipped": False,
            "status": "downloaded",
        },
        "attach": {"ok": "true"},
    }
    job = {
        "job_id": "scihub_malformed",
        "status": "succeeded",
        "result_json": json.dumps(payload),
    }

    batch = compact_batch(
        {"ok": True, "processed": 1, "failed": 0, "results": [job]},
        job_type="scihub_pdf",
    )

    assert batch["pdf_found"] == 0
    assert batch["scihub_attached"] == 0
    assert batch["scihub_downloaded"] == 0


def test_compact_researchgate_summary_rejects_truthy_success_flags() -> None:
    payload = {
        "ok": "true",
        "status": "attached",
        "download": {
            "ok": "true",
            "skipped": False,
            "status": "downloaded",
        },
        "attach": {"ok": "true"},
    }
    job = {
        "job_id": "researchgate_malformed",
        "status": "succeeded",
        "result_json": json.dumps(payload),
    }

    batch = compact_batch(
        {"ok": True, "processed": 1, "failed": 0, "results": [job]},
        job_type="researchgate_pdf",
    )

    assert batch["pdf_found"] == 0
    assert batch["researchgate_attached"] == 0
    assert batch["researchgate_downloaded"] == 0


def test_compact_provider_summary_rejects_malformed_skipped_flag() -> None:
    payload = {
        "ok": False,
        "status": "download_failed",
        "download": {
            "ok": True,
            "skipped": "false",
            "status": "downloaded",
        },
    }
    job = {
        "job_id": "scihub_malformed_skip",
        "status": "failed_retryable",
        "result_json": json.dumps(payload),
    }

    batch = compact_batch(
        {"ok": False, "processed": 1, "failed": 1, "results": [job]},
        job_type="scihub_pdf",
    )

    assert batch["scihub_downloaded"] == 0


def test_full_text_queue_summary_recovers_expired_jobs(monkeypatch) -> None:
    calls: list[str] = []

    class FakeState:
        def recover_expired_metadata_jobs(self, *, job_type: str) -> int:
            calls.append(job_type)
            return 2

        def metadata_queue_summary(self, *, job_type: str) -> dict[str, int]:
            assert job_type == "full_text"
            return {
                "queued": 2,
                "running": 0,
                "succeeded": 0,
                "failed_retryable": 0,
                "failed_final": 0,
            }

    class FakeProcessor:
        def __init__(self, config: object) -> None:
            self.config = config
            self.state = FakeState()

    monkeypatch.setattr(drain_full_text_compact, "from_env", _FakeWorkerConfig)
    monkeypatch.setattr(
        drain_full_text_compact, "ZoteroMetadataProcessor", FakeProcessor
    )

    summary = drain_full_text_compact._full_text_queue_summary()

    assert calls == ["full_text"]
    assert summary["queued"] == 2


def test_full_text_queue_summary_counts_owned_running_jobs(monkeypatch) -> None:
    class FakeState:
        def recover_expired_metadata_jobs(self, *, job_type: str) -> int:
            assert job_type == "full_text"
            return 0

        def metadata_queue_summary(self, *, job_type: str) -> dict[str, int]:
            assert job_type == "full_text"
            return {
                "queued": 5,
                "running": 3,
                "succeeded": 0,
                "failed_retryable": 0,
                "failed_final": 0,
            }

        def list_metadata_jobs(
            self,
            *,
            job_type: str,
            statuses: set[str],
            limit: int,
        ) -> list[dict[str, object]]:
            assert job_type == "full_text"
            assert statuses == {"running"}
            assert limit is None
            return [
                {"lease_owner": "owner-a"},
                {"lease_owner": "owner-b"},
                {"lease_owner": "owner-a"},
            ]

    class FakeProcessor:
        def __init__(self, config: object) -> None:
            self.config = config
            self.state = FakeState()

    monkeypatch.setattr(drain_full_text_compact, "from_env", _FakeWorkerConfig)
    monkeypatch.setattr(
        drain_full_text_compact, "ZoteroMetadataProcessor", FakeProcessor
    )

    summary = drain_full_text_compact._full_text_queue_summary(lease_owner="owner-a")

    assert summary["queued"] == 5
    assert summary["owned_running"] == 2


def test_parallel_run_processes_limit_inside_one_process(monkeypatch, tmp_path) -> None:
    state = {"remaining": 6, "next_id": 0}
    lock = threading.Lock()

    class FakeProcessor:
        def __init__(self, config: object) -> None:
            self.config = config

        def drain_full_text_queue(
            self, *, limit: int, dry_run: bool
        ) -> dict[str, object]:
            del dry_run
            with lock:
                processed = min(limit, state["remaining"])
                state["remaining"] -= processed
                start = state["next_id"]
                state["next_id"] += processed
            jobs = [
                {
                    "job_id": f"job-{index}",
                    "parent_item_key": f"PARENT{index}",
                    "attachment_key": f"PARENT{index}",
                    "status": "succeeded",
                    "result_json": json.dumps({"worker_status": "unresolved"}),
                }
                for index in range(start, start + processed)
            ]
            return {
                "ok": True,
                "processed": processed,
                "failed": 0,
                "queue": {
                    "queued": state["remaining"],
                    "running": 0,
                    "succeeded": state["next_id"],
                    "failed_retryable": 0,
                    "failed_final": 0,
                },
                "results": jobs,
            }

    monkeypatch.setattr(drain_full_text_compact, "from_env", _FakeWorkerConfig)
    monkeypatch.setattr(
        drain_full_text_compact, "ZoteroMetadataProcessor", FakeProcessor
    )

    log_path = tmp_path / "progress.jsonl"
    args = drain_full_text_compact.parse_args(
        [
            "--limit",
            "6",
            "--batch-size",
            "1",
            "--workers",
            "3",
            "--worker-stagger-seconds",
            "0",
            "--log-path",
            str(log_path),
        ]
    )

    assert drain_full_text_compact.run(args) == 0

    records = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    summary = records[-1]
    batches = records[:-1]

    assert summary["done"] is True
    assert summary["workers"] == 3
    assert summary["processed"] == 6
    assert summary["failed"] == 0
    assert sum(batch["processed"] for batch in batches) == 6
    assert all("worker_index" in batch for batch in batches)


def test_outer_parallel_drain_forces_each_processor_pool_to_one(monkeypatch) -> None:
    base = drain_full_text_compact.from_env(load_file=False)
    configured = replace(base, metadata_drain_max_workers=7)
    captured: list[int] = []

    class FakeProcessor:
        def __init__(self, config) -> None:
            captured.append(config.metadata_drain_max_workers)

    monkeypatch.setattr(
        drain_full_text_compact,
        "from_env",
        lambda: configured,
    )
    monkeypatch.setattr(
        drain_full_text_compact,
        "ZoteroMetadataProcessor",
        FakeProcessor,
    )

    drain_full_text_compact._single_worker_processor()

    assert captured == [1]


def test_dynamic_run_does_not_start_more_batches_than_queue(
    monkeypatch, tmp_path
) -> None:
    state = {
        "remaining": 2,
        "next_id": 0,
        "active": 0,
        "max_active": 0,
        "drain_calls": 0,
    }
    lock = threading.Lock()

    class FakeState:
        def metadata_queue_summary(self, *, job_type: str) -> dict[str, int]:
            assert job_type == "full_text"
            with lock:
                return {
                    "queued": state["remaining"] + state["active"],
                    "running": state["active"],
                    "succeeded": state["next_id"],
                    "failed_retryable": 0,
                    "failed_final": 0,
                }

    class FakeProcessor:
        def __init__(self, config: object) -> None:
            self.config = config
            self.state = FakeState()

        def drain_full_text_queue(
            self, *, limit: int, dry_run: bool
        ) -> dict[str, object]:
            del dry_run
            with lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
                state["drain_calls"] += 1
                processed = min(limit, state["remaining"])
                state["remaining"] -= processed
                start = state["next_id"]
                state["next_id"] += processed
            time.sleep(0.01)
            with lock:
                state["active"] -= 1
                queued = state["remaining"]
                succeeded = state["next_id"]
            jobs = [
                {
                    "job_id": f"job-{index}",
                    "parent_item_key": f"PARENT{index}",
                    "attachment_key": f"PARENT{index}",
                    "status": "succeeded",
                    "result_json": json.dumps({"worker_status": "unresolved"}),
                }
                for index in range(start, start + processed)
            ]
            return {
                "ok": True,
                "processed": processed,
                "failed": 0,
                "queue": {
                    "queued": queued,
                    "running": state["active"],
                    "succeeded": succeeded,
                    "failed_retryable": 0,
                    "failed_final": 0,
                },
                "results": jobs,
            }

    monkeypatch.setattr(drain_full_text_compact, "from_env", _FakeWorkerConfig)
    monkeypatch.setattr(
        drain_full_text_compact, "ZoteroMetadataProcessor", FakeProcessor
    )

    log_path = tmp_path / "dynamic-small.jsonl"
    args = drain_full_text_compact.parse_args(
        [
            "--limit",
            "0",
            "--batch-size",
            "1",
            "--workers",
            "64",
            "--dynamic-workers",
            "--dynamic-poll-seconds",
            "0.01",
            "--log-path",
            str(log_path),
        ]
    )

    assert drain_full_text_compact.run(args) == 0

    records = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    summary = records[-1]
    batches = records[:-1]

    assert summary["dynamic"] is True
    assert summary["max_workers"] == 64
    assert summary["processed"] == 2
    assert state["drain_calls"] == 2
    assert state["max_active"] <= 2
    assert sum(batch["processed"] for batch in batches) == 2


def test_dynamic_run_caps_active_batches_at_worker_limit(monkeypatch, tmp_path) -> None:
    state = {
        "remaining": 12,
        "next_id": 0,
        "active": 0,
        "max_active": 0,
        "drain_calls": 0,
    }
    lock = threading.Lock()

    class FakeState:
        def metadata_queue_summary(self, *, job_type: str) -> dict[str, int]:
            assert job_type == "full_text"
            with lock:
                return {
                    "queued": state["remaining"],
                    "running": state["active"],
                    "succeeded": state["next_id"],
                    "failed_retryable": 0,
                    "failed_final": 0,
                }

    class FakeProcessor:
        def __init__(self, config: object) -> None:
            self.config = config
            self.state = FakeState()

        def drain_full_text_queue(
            self, *, limit: int, dry_run: bool
        ) -> dict[str, object]:
            del dry_run
            with lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
                state["drain_calls"] += 1
                processed = min(limit, state["remaining"])
                state["remaining"] -= processed
                start = state["next_id"]
                state["next_id"] += processed
            time.sleep(0.02)
            with lock:
                state["active"] -= 1
                queued = state["remaining"]
                running = state["active"]
                succeeded = state["next_id"]
            jobs = [
                {
                    "job_id": f"job-{index}",
                    "parent_item_key": f"PARENT{index}",
                    "attachment_key": f"PARENT{index}",
                    "status": "succeeded",
                    "result_json": json.dumps({"worker_status": "unresolved"}),
                }
                for index in range(start, start + processed)
            ]
            return {
                "ok": True,
                "processed": processed,
                "failed": 0,
                "queue": {
                    "queued": queued,
                    "running": running,
                    "succeeded": succeeded,
                    "failed_retryable": 0,
                    "failed_final": 0,
                },
                "results": jobs,
            }

    monkeypatch.setattr(drain_full_text_compact, "from_env", _FakeWorkerConfig)
    monkeypatch.setattr(
        drain_full_text_compact, "ZoteroMetadataProcessor", FakeProcessor
    )

    log_path = tmp_path / "dynamic-cap.jsonl"
    args = drain_full_text_compact.parse_args(
        [
            "--limit",
            "0",
            "--batch-size",
            "1",
            "--workers",
            "4",
            "--dynamic-workers",
            "--dynamic-poll-seconds",
            "0.01",
            "--log-path",
            str(log_path),
        ]
    )

    assert drain_full_text_compact.run(args) == 0

    records = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    summary = records[-1]

    assert summary["dynamic"] is True
    assert summary["max_workers"] == 4
    assert summary["processed"] == 12
    assert state["drain_calls"] == 12
    assert state["max_active"] <= 4


def test_dynamic_scihub_run_uses_scihub_queue(monkeypatch, tmp_path) -> None:
    state = {
        "remaining": 2,
        "next_id": 0,
        "active": 0,
        "max_active": 0,
        "scihub_calls": 0,
    }
    lock = threading.Lock()

    class FakeState:
        def recover_expired_metadata_jobs(self, *, job_type: str) -> int:
            assert job_type == "scihub_pdf"
            return 0

        def metadata_queue_summary(self, *, job_type: str) -> dict[str, int]:
            assert job_type == "scihub_pdf"
            with lock:
                return {
                    "queued": state["remaining"] + state["active"],
                    "running": state["active"],
                    "succeeded": state["next_id"],
                    "failed_retryable": 0,
                    "failed_final": 0,
                }

    class FakeProcessor:
        def __init__(self, config: object) -> None:
            self.config = config
            self.state = FakeState()

        def drain_scihub_pdf_queue(
            self, *, limit: int, dry_run: bool
        ) -> dict[str, object]:
            del dry_run
            with lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
                state["scihub_calls"] += 1
                processed = min(limit, state["remaining"])
                state["remaining"] -= processed
                start = state["next_id"]
                state["next_id"] += processed
            time.sleep(0.01)
            with lock:
                state["active"] -= 1
                queued = state["remaining"]
                running = state["active"]
                succeeded = state["next_id"]
            jobs = [
                {
                    "job_id": f"scihub-{index}",
                    "parent_item_key": f"PARENT{index}",
                    "attachment_key": f"PARENT{index}",
                    "status": "succeeded",
                    "result_json": json.dumps(
                        {
                            "ok": True,
                            "status": "attached",
                            "download": {
                                "ok": True,
                                "status": "downloaded",
                                "doi": f"10.123/{index}",
                                "output_path": f"/tmp/{index}.pdf",
                            },
                            "attach": {
                                "ok": True,
                                "relay": {"newAttachmentKey": f"PDF{index}"},
                            },
                        }
                    ),
                }
                for index in range(start, start + processed)
            ]
            return {
                "ok": True,
                "processed": processed,
                "failed": 0,
                "queue": {
                    "queued": queued,
                    "running": running,
                    "succeeded": succeeded,
                    "failed_retryable": 0,
                    "failed_final": 0,
                },
                "results": jobs,
            }

    monkeypatch.setattr(drain_full_text_compact, "from_env", _FakeWorkerConfig)
    monkeypatch.setattr(
        drain_full_text_compact, "ZoteroMetadataProcessor", FakeProcessor
    )

    log_path = tmp_path / "scihub-dynamic.jsonl"
    args = drain_full_text_compact.parse_args(
        [
            "--job-type",
            "scihub_pdf",
            "--limit",
            "0",
            "--batch-size",
            "1",
            "--workers",
            "64",
            "--dynamic-workers",
            "--dynamic-poll-seconds",
            "0.01",
            "--log-path",
            str(log_path),
        ]
    )

    assert drain_full_text_compact.run(args) == 0

    records = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    summary = records[-1]

    assert summary["job_type"] == "scihub_pdf"
    assert summary["dynamic"] is True
    assert summary["processed"] == 2
    assert summary["pdf_found"] == 2
    assert summary["scihub_attached"] == 2
    assert summary["scihub_downloaded"] == 2
    assert state["scihub_calls"] == 2
    assert state["max_active"] <= 2


def test_full_text_pdf_cycle_scans_scihub_after_researchgate(
    monkeypatch, tmp_path
) -> None:
    calls: list[str] = []

    class FakeProcessor:
        def __init__(self, _config: object) -> None:
            pass

        def drain_full_text_queue(
            self, *, limit: int, dry_run: bool
        ) -> dict[str, object]:
            calls.append("full_text")
            return {
                "ok": True,
                "processed": 0,
                "failed": 0,
                "queue": {},
                "results": [],
            }

        def drain_researchgate_pdf_queue(
            self, *, limit: int, dry_run: bool
        ) -> dict[str, object]:
            calls.append("researchgate_pdf")
            return {
                "ok": True,
                "processed": 0,
                "failed": 0,
                "queue": {},
                "results": [],
            }

        def scihub_pdf_backlog_scan(
            self, *, limit: int | None, force: bool
        ) -> dict[str, object]:
            calls.append("scihub_pdf_backlog_scan")
            return {
                "ok": True,
                "scanned": 3,
                "queued": 2,
                "skipped": 1,
                "queue": {"queued": 2},
            }

        def drain_scihub_pdf_queue(
            self, *, limit: int, dry_run: bool
        ) -> dict[str, object]:
            calls.append("scihub_pdf")
            return {
                "ok": True,
                "processed": 0,
                "failed": 0,
                "queue": {},
                "results": [],
            }

    monkeypatch.setattr(drain_full_text_compact, "from_env", _FakeWorkerConfig)
    monkeypatch.setattr(
        drain_full_text_compact, "ZoteroMetadataProcessor", FakeProcessor
    )

    log_path = tmp_path / "cycle.jsonl"
    args = drain_full_text_compact.parse_args(
        [
            "--job-type",
            "full_text_pdf_cycle",
            "--log-path",
            str(log_path),
        ]
    )

    assert drain_full_text_compact.run(args) == 0

    records = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert calls == [
        "full_text",
        "researchgate_pdf",
        "scihub_pdf_backlog_scan",
        "scihub_pdf",
    ]
    assert records[-1]["job_type"] == "full_text_pdf_cycle"
    assert [stage["job_type"] for stage in records[-1]["stages"]] == [
        "full_text",
        "researchgate_pdf",
        "scihub_pdf_backlog_scan",
        "scihub_pdf",
    ]


def test_full_text_pdf_cycle_dry_run_does_not_enqueue_scihub_backlog(
    monkeypatch,
    tmp_path,
) -> None:
    class FakeProcessor:
        def __init__(self, _config) -> None:
            pass

        @staticmethod
        def _preview(job_type: str) -> dict[str, object]:
            return {
                "ok": True,
                "dry_run": True,
                "processed": 0,
                "failed": 0,
                "queue": {"queued": 0},
                "results": [],
                "job_type": job_type,
            }

        def drain_full_text_queue(self, *, limit: int, dry_run: bool):
            assert limit > 0 and dry_run is True
            return self._preview("full_text")

        def drain_researchgate_pdf_queue(self, *, limit: int, dry_run: bool):
            assert limit > 0 and dry_run is True
            return self._preview("researchgate_pdf")

        def drain_scihub_pdf_queue(self, *, limit: int, dry_run: bool):
            assert limit > 0 and dry_run is True
            return self._preview("scihub_pdf")

        def scihub_pdf_backlog_scan(self, **_kwargs):
            raise AssertionError("dry-run must not enqueue Sci-Hub backlog jobs")

    monkeypatch.setattr(drain_full_text_compact, "from_env", _FakeWorkerConfig)
    monkeypatch.setattr(
        drain_full_text_compact, "ZoteroMetadataProcessor", FakeProcessor
    )
    log_path = tmp_path / "cycle-dry-run.jsonl"
    args = drain_full_text_compact.parse_args(
        [
            "--job-type",
            "full_text_pdf_cycle",
            "--dry-run",
            "--log-path",
            str(log_path),
        ]
    )

    assert drain_full_text_compact.run(args) == 0
    records = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    scan = next(
        record for record in records if record["job_type"] == "scihub_pdf_backlog_scan"
    )
    assert scan["dry_run"] is True
    assert scan["skipped"] is True
    assert scan["reason"] == "dry_run_intake_disabled"


def _successful_drain_result(
    *,
    start: int,
    processed: int,
    queued: int,
    running: int = 0,
) -> dict[str, object]:
    jobs = [
        {
            "job_id": f"job-{index}",
            "parent_item_key": f"PARENT{index}",
            "attachment_key": f"PARENT{index}",
            "status": "succeeded",
            "result_json": json.dumps({"worker_status": "unresolved"}),
        }
        for index in range(start, start + processed)
    ]
    return {
        "ok": True,
        "processed": processed,
        "failed": 0,
        "queue": {
            "queued": queued,
            "running": running,
            "succeeded": start + processed,
            "failed_retryable": 0,
            "failed_final": 0,
        },
        "results": jobs,
    }


def test_provider_failed_final_jobs_return_nonzero_exit_code() -> None:
    assert drain_full_text_compact._job_exit_code(failed=1, batch_errors=0) == 1


def test_parallel_short_batch_releases_capacity_without_stopping_other_work(
    monkeypatch,
    tmp_path,
) -> None:
    state = {"remaining": 3, "next_id": 0, "calls": 0}
    lock = threading.Lock()

    class FakeProcessor:
        def __init__(self, config: object) -> None:
            self.config = config

        def drain_full_text_queue(
            self, *, limit: int, dry_run: bool
        ) -> dict[str, object]:
            assert limit > 0
            assert dry_run is False
            with lock:
                state["calls"] += 1
                processed = 1 if state["remaining"] > 0 else 0
                state["remaining"] -= processed
                start = state["next_id"]
                state["next_id"] += processed
                queued = state["remaining"]
            return _successful_drain_result(
                start=start,
                processed=processed,
                queued=queued,
            )

    monkeypatch.setattr(drain_full_text_compact, "from_env", _FakeWorkerConfig)
    monkeypatch.setattr(
        drain_full_text_compact, "ZoteroMetadataProcessor", FakeProcessor
    )
    log_path = tmp_path / "parallel-short-batch.jsonl"
    args = drain_full_text_compact.parse_args(
        [
            "--limit",
            "3",
            "--batch-size",
            "2",
            "--workers",
            "2",
            "--worker-stagger-seconds",
            "0.1",
            "--log-path",
            str(log_path),
        ]
    )

    assert drain_full_text_compact.run(args) == 0

    records = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert records[-1]["processed"] == 3
    assert state == {"remaining": 0, "next_id": 3, "calls": 3}


def test_dynamic_run_releases_unused_finite_reservation(
    monkeypatch,
    tmp_path,
) -> None:
    state = {"remaining": 2, "next_id": 0, "calls": 0}
    lock = threading.Lock()

    class FakeState:
        def metadata_queue_summary(self, *, job_type: str) -> dict[str, int]:
            assert job_type == "full_text"
            with lock:
                return {
                    "queued": state["remaining"],
                    "running": 0,
                    "succeeded": state["next_id"],
                    "failed_retryable": 0,
                    "failed_final": 0,
                }

    class FakeProcessor:
        def __init__(self, config: object) -> None:
            self.config = config
            self.state = FakeState()

        def drain_full_text_queue(
            self, *, limit: int, dry_run: bool
        ) -> dict[str, object]:
            assert limit > 0
            assert dry_run is False
            with lock:
                state["calls"] += 1
                processed = 1 if state["remaining"] > 0 else 0
                state["remaining"] -= processed
                start = state["next_id"]
                state["next_id"] += processed
                queued = state["remaining"]
            return _successful_drain_result(
                start=start,
                processed=processed,
                queued=queued,
            )

    monkeypatch.setattr(drain_full_text_compact, "from_env", _FakeWorkerConfig)
    monkeypatch.setattr(
        drain_full_text_compact, "ZoteroMetadataProcessor", FakeProcessor
    )
    log_path = tmp_path / "dynamic-reservation.jsonl"
    args = drain_full_text_compact.parse_args(
        [
            "--limit",
            "2",
            "--batch-size",
            "2",
            "--workers",
            "2",
            "--dynamic-workers",
            "--dynamic-poll-seconds",
            "0.01",
            "--log-path",
            str(log_path),
        ]
    )

    assert drain_full_text_compact.run(args) == 0

    records = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert records[-1]["processed"] == 2
    assert state == {"remaining": 0, "next_id": 2, "calls": 2}


def test_dynamic_finite_run_waits_for_foreign_running_job(
    monkeypatch,
    tmp_path,
) -> None:
    state = {
        "foreign_running": True,
        "remaining": 0,
        "next_id": 0,
        "sleep_calls": 0,
    }
    lock = threading.Lock()

    class FakeState:
        def metadata_queue_summary(self, *, job_type: str) -> dict[str, int]:
            assert job_type == "full_text"
            with lock:
                return {
                    "queued": state["remaining"],
                    "running": 1 if state["foreign_running"] else 0,
                    "succeeded": state["next_id"],
                    "failed_retryable": 0,
                    "failed_final": 0,
                }

    class FakeProcessor:
        def __init__(self, config: object) -> None:
            self.config = config
            self.state = FakeState()

        def drain_full_text_queue(
            self, *, limit: int, dry_run: bool
        ) -> dict[str, object]:
            assert limit > 0
            assert dry_run is False
            with lock:
                processed = min(limit, state["remaining"])
                state["remaining"] -= processed
                start = state["next_id"]
                state["next_id"] += processed
                queued = state["remaining"]
            return _successful_drain_result(
                start=start,
                processed=processed,
                queued=queued,
            )

    def complete_foreign_job(_seconds: float) -> None:
        with lock:
            state["sleep_calls"] += 1
            if state["foreign_running"]:
                state["foreign_running"] = False
                state["remaining"] = 1

    monkeypatch.setattr(drain_full_text_compact, "from_env", _FakeWorkerConfig)
    monkeypatch.setattr(
        drain_full_text_compact, "ZoteroMetadataProcessor", FakeProcessor
    )
    monkeypatch.setattr(drain_full_text_compact.time, "sleep", complete_foreign_job)
    log_path = tmp_path / "dynamic-foreign-running.jsonl"
    args = drain_full_text_compact.parse_args(
        [
            "--limit",
            "2",
            "--batch-size",
            "1",
            "--workers",
            "2",
            "--dynamic-workers",
            "--dynamic-poll-seconds",
            "0.01",
            "--log-path",
            str(log_path),
        ]
    )

    assert drain_full_text_compact.run(args) == 0

    records = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert state["sleep_calls"] >= 1
    assert state["foreign_running"] is False
    assert records[-1]["processed"] == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("processed", True),
        ("processed", "1"),
        ("processed", -1),
        ("failed", True),
        ("failed", "1"),
        ("failed", -1),
    ],
)
def test_compact_batch_rejects_malformed_counts(field: str, value: object) -> None:
    result: dict[str, object] = {
        "ok": True,
        "processed": 1,
        "failed": 0,
        "queue": {},
        "results": [],
    }
    result[field] = value

    with pytest.raises(RuntimeError, match=field):
        compact_batch(result)


def test_compact_batch_rejects_failed_count_above_processed() -> None:
    with pytest.raises(RuntimeError, match="failed.*processed"):
        compact_batch(
            {
                "ok": False,
                "processed": 0,
                "failed": 1,
                "queue": {},
                "results": [],
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("results", {}),
        ("results", [None]),
        ("results", ["not-a-job"]),
        ("queue", []),
    ],
)
def test_compact_batch_rejects_malformed_result_structure(
    field: str,
    value: object,
) -> None:
    result: dict[str, object] = {
        "ok": True,
        "processed": 0,
        "failed": 0,
        "queue": {},
        "results": [],
    }
    result[field] = value

    with pytest.raises(RuntimeError, match=field):
        compact_batch(result)


@pytest.mark.parametrize(
    "argv",
    [
        ["--limit", "-1"],
        ["--batch-size", "0"],
        ["--batch-size", "-1"],
        ["--sleep-seconds", "nan"],
        ["--worker-stagger-seconds", "inf"],
        ["--dynamic-poll-seconds", "nan"],
        ["--workers", "0"],
        ["--sleep-seconds", "-0.1"],
        ["--worker-stagger-seconds", "-0.1"],
        ["--max-failures", "-1"],
        ["--max-retryable-failures", "-1"],
        ["--dynamic-poll-seconds", "0"],
        ["--dynamic-poll-seconds", "-0.1"],
        ["--log-path", ""],
    ],
)
def test_parse_args_rejects_invalid_operational_controls(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        drain_full_text_compact.parse_args(argv)

    assert exc_info.value.code == 2


def test_parse_args_accepts_documented_zero_controls() -> None:
    args = drain_full_text_compact.parse_args(
        [
            "--limit",
            "0",
            "--max-failures",
            "0",
            "--max-retryable-failures",
            "0",
            "--sleep-seconds",
            "0",
            "--worker-stagger-seconds",
            "0",
        ]
    )

    assert args.limit == 0
    assert args.max_failures == 0
    assert args.max_retryable_failures == 0


def test_compact_batch_requires_one_result_per_processed_job() -> None:
    with pytest.raises(RuntimeError, match="results.*processed"):
        compact_batch(
            {
                "ok": True,
                "processed": 1,
                "failed": 0,
                "queue": {},
                "results": [],
            }
        )


def test_compact_batch_requires_failed_status_count_to_match() -> None:
    with pytest.raises(RuntimeError, match="failed.*status"):
        compact_batch(
            {
                "ok": False,
                "processed": 1,
                "failed": 1,
                "queue": {},
                "results": [
                    {
                        "job_id": "job-1",
                        "status": "succeeded",
                        "result_json": json.dumps({"worker_status": "unresolved"}),
                    }
                ],
            }
        )


def test_console_json_output_is_ascii_safe(monkeypatch) -> None:
    output = io.BytesIO()
    stream = io.TextIOWrapper(output, encoding="ascii", errors="strict")
    monkeypatch.setattr(drain_full_text_compact.sys, "stdout", stream)

    drain_full_text_compact._print_json({"message": "Привет"})
    stream.flush()

    assert b"\\u041f" in output.getvalue()


def test_compact_batch_requires_exact_boolean_ok() -> None:
    with pytest.raises(RuntimeError, match="batch ok"):
        compact_batch(
            {
                "ok": "false",
                "processed": 0,
                "failed": 0,
                "queue": {},
                "results": [],
            }
        )


def test_batch_level_error_stops_parallel_coordinator(tmp_path) -> None:
    coordinator = drain_full_text_compact.ParallelDrainCoordinator(
        log_path=tmp_path / "coordinator.jsonl",
        limit=None,
        batch_size=1,
        max_failures=0,
        max_retryable_failures=0,
    )
    compact = compact_batch(
        {
            "ok": False,
            "error": "relay unavailable",
            "processed": 0,
            "failed": 0,
            "queue": {"queued": 1},
            "results": [],
        }
    )

    coordinator.record(compact)

    assert compact["batch_error"] is True
    assert coordinator.total_batch_errors == 1
    assert coordinator.should_stop() is True


def test_single_run_returns_nonzero_for_batch_level_error(
    monkeypatch,
    tmp_path,
) -> None:
    class FakeProcessor:
        def __init__(self, config: object) -> None:
            self.config = config

        def drain_full_text_queue(
            self, *, limit: int, dry_run: bool
        ) -> dict[str, object]:
            assert limit > 0
            assert dry_run is False
            return {
                "ok": False,
                "error": "relay unavailable",
                "queue": {
                    "queued": 1,
                    "running": 0,
                    "succeeded": 0,
                    "failed_retryable": 0,
                    "failed_final": 0,
                },
            }

    monkeypatch.setattr(drain_full_text_compact, "from_env", _FakeWorkerConfig)
    monkeypatch.setattr(
        drain_full_text_compact, "ZoteroMetadataProcessor", FakeProcessor
    )
    log_path = tmp_path / "single-preflight-error.jsonl"
    args = drain_full_text_compact.parse_args(["--log-path", str(log_path)])

    assert drain_full_text_compact.run(args) == 1

    records = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert records[-1]["batch_errors"] == 1
