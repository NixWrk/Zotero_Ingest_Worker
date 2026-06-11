from __future__ import annotations

from typing import Any

from zotero_ingest_worker import metadata_processor
from zotero_ingest_worker import metadata_processor_helpers
from zotero_ingest_worker.full_run import FullRunManager
from zotero_ingest_worker.full_run_options import FullRunOptions
from zotero_ingest_worker.full_run_plan import next_ingest_action
from zotero_ingest_worker.full_run_results import _result_summary
from zotero_ingest_worker.metadata_processor import ZoteroMetadataProcessor


def test_metadata_processor_reexports_helper_functions_for_legacy_imports() -> None:
    assert metadata_processor.build_metadata_diff is metadata_processor_helpers.build_metadata_diff
    assert metadata_processor.extract_doi_from_text is metadata_processor_helpers.extract_doi_from_text
    assert metadata_processor._scihub_query_candidates is metadata_processor_helpers._scihub_query_candidates


def test_full_run_manager_uses_extracted_action_policy() -> None:
    options = FullRunOptions(full_text_drain=True, researchgate_pdf_drain=True)
    queues: dict[str, Any] = {
        "metadata_queue": {"queued": 0},
        "full_text_queue": {"queued": 0},
        "researchgate_pdf_queue": {"queued": 1},
    }

    assert next_ingest_action(options, **queues) == "researchgate_pdf"
    assert FullRunManager._next_action(options, **queues) == "researchgate_pdf"


def test_result_summary_keeps_only_controller_event_fields() -> None:
    summary = _result_summary(
        {
            "ok": True,
            "processed": 2,
            "failed": 1,
            "large_payload": {"ignored": True},
        }
    )

    assert summary == {"ok": True, "processed": 2, "failed": 1}


def test_full_text_backlog_scan_delegates_to_scanner(monkeypatch: Any) -> None:
    calls: list[dict[str, Any]] = []

    def fake_scan(processor: ZoteroMetadataProcessor, **kwargs: Any) -> dict[str, Any]:
        calls.append({"processor": processor, **kwargs})
        return {"ok": True, "mode": "fake_full_text_backlog_scan"}

    monkeypatch.setattr(metadata_processor, "scan_full_text_backlog", fake_scan)
    processor = object.__new__(ZoteroMetadataProcessor)

    result = processor.full_text_backlog_scan(limit=7, force=True, collection="To ingest")

    assert result == {"ok": True, "mode": "fake_full_text_backlog_scan"}
    assert calls == [
        {
            "processor": processor,
            "max_items": None,
            "limit": 7,
            "force": True,
            "library_id": None,
            "data_dir": None,
            "collection": "To ingest",
        }
    ]
