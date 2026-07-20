from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import zotero_ingest_worker.metadata_processor as metadata_processor_module
import zotero_ingest_worker.metadata_backlog_scanner as backlog_scanner_module
import zotero_ingest_worker.metadata_processor_helpers as metadata_helpers_module
import zotero_ingest_worker.full_text_attachment as full_text_attachment_module
from zotero_ingest_worker.local_zotero import (
    LocalAttachment,
    LocalItemMetadata,
    LocalZoteroStore,
)
from zotero_ingest_worker.arxiv_html import ArxivHtmlValidationError
from zotero_ingest_worker.metadata_jobs import METADATA_JOB_FULL_TEXT
from zotero_ingest_worker.metadata_processor import (
    MetadataCandidate,
    _best_successful_html_download,
    _html_attachment_source_with_embedded_assets,
    _is_nonretryable_worker_error,
    _nonactionable_metadata_http_error_reason,
    _relay_url_candidates,
    arxiv_html_filename,
    build_metadata_diff,
    build_metadata_patch,
    extract_arxiv_id_from_text,
    extract_doi_from_text,
    filter_metadata_diff_for_item_type,
    full_text_worker_status,
    parse_arxiv_atom,
    title_match_score,
    validate_arxiv_html,
    zotero_translator_item_to_candidate,
    ZoteroMetadataProcessor,
)
from zotero_ingest_worker.relay_client import ZoteroRelayClient
from zotero_ingest_worker.state import FileSignature, OcrStateStore


def test_local_zotero_iter_pdf_attachments_accepts_unbounded_limit(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "Zotero_Test_Data"
    storage_dir = data_dir / "storage"
    for key, filename in {
        "PDFNEW": "new.pdf",
        "PDFOLD": "old.pdf",
        "ORPHAN": "orphan.pdf",
    }.items():
        folder = storage_dir / key
        folder.mkdir(parents=True)
        (folder / filename).write_bytes(b"%PDF")
    sqlite_path = data_dir / "zotero.sqlite"
    connection = sqlite3.connect(sqlite_path)
    try:
        connection.executescript(
            """
            create table items (
                itemID integer primary key,
                key text not null,
                dateModified timestamp
            );
            create table itemAttachments (
                itemID integer primary key,
                parentItemID int,
                linkMode int,
                contentType text,
                path text
            );
            create table deletedItems (
                itemID int primary key
            );
            insert into items (itemID, key, dateModified) values
                (1, 'PDFOLD', '2026-01-01'),
                (2, 'PDFNEW', '2026-01-02');
            insert into itemAttachments
                (itemID, parentItemID, linkMode, contentType, path)
                values
                (1, null, 0, 'application/pdf', 'storage:old.pdf'),
                (2, null, 0, 'application/pdf', 'storage:new.pdf');
            """
        )
        connection.commit()
    finally:
        connection.close()

    store = LocalZoteroStore(
        SimpleNamespace(
            zotero_data_dir=data_dir,
            zotero_sqlite_path=sqlite_path,
            resolved_storage_dir=storage_dir,
        )
    )

    assert [item.key for item in store.iter_pdf_attachments(max_items=None)] == [
        "PDFNEW",
        "PDFOLD",
        "ORPHAN",
    ]
    assert [item.key for item in store.iter_pdf_attachments(max_items=1)] == ["PDFNEW"]


def _valid_full_article_assessment() -> dict[str, object]:
    return {
        "ok": True,
        "reason": "article_html",
        "text_chars": 25_000,
        "markers": ["article_tag", "article_body"],
        "section_markers": ["abstract", "methods", "results", "references"],
    }


def test_extract_identifiers_from_extended_metadata_text() -> None:
    text = """
    DOI: https://doi.org/10.48550/arXiv.2401.01234v2
    Also available at https://arxiv.org/pdf/cs/9901001.pdf
    """

    assert extract_doi_from_text(text) == "10.48550/arXiv.2401.01234v2"
    assert extract_arxiv_id_from_text(text) == "2401.01234"


def test_parse_arxiv_atom_maps_zotero_like_fields() -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:arxiv="http://arxiv.org/schemas/atom">
      <entry>
        <id>http://arxiv.org/abs/2401.01234v2</id>
        <updated>2024-01-03T00:00:00Z</updated>
        <published>2024-01-01T00:00:00Z</published>
        <title>  A Careful   Metadata Pipeline  </title>
        <summary>  This paper tests metadata. </summary>
        <author><name>Ada Lovelace</name></author>
        <arxiv:primary_category term="cs.DL" scheme="http://arxiv.org/schemas/atom"/>
      </entry>
    </feed>
    """

    candidates = parse_arxiv_atom(xml)

    assert len(candidates) == 1
    fields = candidates[0].fields
    assert candidates[0].identifier == "2401.01234"
    assert fields["title"] == "A Careful Metadata Pipeline"
    assert fields["DOI"] == "10.48550/arXiv.2401.01234"
    assert fields["extra"] == "arXiv:2401.01234 [cs.DL]"
    assert fields["archive"] == "arXiv"
    assert fields["libraryCatalog"] == "arXiv.org"


def test_metadata_patch_respects_policy_and_allowed_fields() -> None:
    candidate = MetadataCandidate(
        source="crossref",
        identifier="10.1000/example",
        score=1.0,
        fields={
            "title": "New title",
            "DOI": "10.1000/example",
            "publicationTitle": "Journal of Examples",
            "extra": "arXiv:2401.01234",
        },
        raw={},
    )

    empty_only = build_metadata_patch(
        candidate,
        current_fields={"title": "Existing title", "extra": "Original line"},
        policy="emptyFieldsOnly",
    )
    overwrite = build_metadata_patch(
        candidate,
        current_fields={"title": "Existing title", "extra": "Original line"},
        policy="allowOverwrite",
    )

    assert empty_only == {
        "DOI": "10.1000/example",
        "publicationTitle": "Journal of Examples",
    }
    assert overwrite["title"] == "New title"
    assert overwrite["publicationTitle"] == "Journal of Examples"
    assert overwrite["extra"] == "Original line\narXiv:2401.01234"


def test_metadata_diff_explains_patch_and_skips() -> None:
    candidate = MetadataCandidate(
        source="crossref",
        identifier="10.1000/example",
        score=1.0,
        fields={
            "title": "Existing title",
            "DOI": "10.1000/example",
            "publicationTitle": "Journal of Examples",
            "url": "",
        },
        raw={},
    )

    diff = build_metadata_diff(
        candidate,
        current_fields={"title": "Existing title"},
        policy="emptyFieldsOnly",
    )

    assert diff["patch"] == {
        "DOI": "10.1000/example",
        "publicationTitle": "Journal of Examples",
    }
    assert diff["skipped_fields"]["title"] == "current_field_not_empty"
    assert diff["skipped_fields"]["url"] == "candidate_empty"


def test_metadata_diff_filters_fields_not_valid_for_preprints() -> None:
    candidate = MetadataCandidate(
        source="crossref",
        identifier="10.48550/arXiv.2401.01234",
        score=1.0,
        fields={
            "DOI": "10.48550/arXiv.2401.01234",
            "ISSN": "1234-5678",
            "publicationTitle": "Journal of Preprints",
            "volume": "12",
            "ISBN": "978-1-2345-6789-0",
            "websiteTitle": "Publisher landing page",
        },
        raw={},
    )
    diff = build_metadata_diff(candidate, current_fields={}, policy="emptyFieldsOnly")

    preprint = filter_metadata_diff_for_item_type(diff, item_type="preprint")
    journal = filter_metadata_diff_for_item_type(diff, item_type="journalArticle")

    assert preprint["patch"] == {"DOI": "10.48550/arXiv.2401.01234"}
    assert preprint["applied_fields"] == ["DOI"]
    assert (
        preprint["skipped_fields"]["ISSN"] == "field_not_valid_for_item_type:preprint"
    )
    assert (
        preprint["skipped_fields"]["publicationTitle"]
        == "field_not_valid_for_item_type:preprint"
    )
    assert (
        preprint["skipped_fields"]["volume"] == "field_not_valid_for_item_type:preprint"
    )
    assert journal["patch"] == {
        "DOI": "10.48550/arXiv.2401.01234",
        "ISSN": "1234-5678",
        "publicationTitle": "Journal of Preprints",
        "volume": "12",
    }
    assert (
        journal["skipped_fields"]["ISBN"]
        == "field_not_valid_for_item_type:journalArticle"
    )
    assert (
        journal["skipped_fields"]["websiteTitle"]
        == "field_not_valid_for_item_type:journalArticle"
    )


def test_metadata_diff_filters_runtime_invalid_fields_for_other_item_types() -> None:
    diff = {
        "patch": {
            "title": "A Better Title",
            "numPages": "12",
            "publisher": "Publisher",
            "ISSN": "1234-5678",
            "libraryCatalog": "Crossref",
            "pages": "1-2",
            "bookTitle": "Proceedings Book",
        },
        "skipped_fields": {},
        "applied_fields": [],
    }

    book_section = filter_metadata_diff_for_item_type(diff, item_type="bookSection")
    report = filter_metadata_diff_for_item_type(diff, item_type="report")
    webpage = filter_metadata_diff_for_item_type(diff, item_type="webpage")
    conference = filter_metadata_diff_for_item_type(diff, item_type="conferencePaper")
    document = filter_metadata_diff_for_item_type(diff, item_type="document")
    patent = filter_metadata_diff_for_item_type(diff, item_type="patent")

    assert "numPages" not in book_section["patch"]
    assert book_section["patch"]["bookTitle"] == "Proceedings Book"
    assert "publisher" not in report["patch"]
    assert report["patch"]["pages"] == "1-2"
    assert "ISSN" not in webpage["patch"]
    assert "libraryCatalog" not in webpage["patch"]
    assert "pages" not in webpage["patch"]
    assert "bookTitle" not in conference["patch"]
    assert conference["patch"]["title"] == "A Better Title"
    assert "pages" not in document["patch"]
    assert document["patch"]["title"] == "A Better Title"
    assert "libraryCatalog" not in patent["patch"]
    assert patent["patch"]["title"] == "A Better Title"


def test_zotero_translator_item_maps_translation_server_payload() -> None:
    item = {
        "itemType": "journalArticle",
        "title": "A Careful Metadata Pipeline",
        "abstractNote": "<p>This paper tests metadata.</p>",
        "date": "2024",
        "DOI": "https://doi.org/10.1000/example",
        "url": "https://example.org/paper",
        "publicationTitle": "Journal of Pipelines",
        "journalAbbreviation": "J. Pipe.",
        "ISSN": ["1234-5678", "8765-4321"],
        "volume": "12",
        "issue": "3",
        "pages": "1-9",
        "extra": "arXiv:2401.01234 [cs.DL]",
        "creators": [{"firstName": "Ada", "lastName": "Lovelace"}],
    }

    candidate = zotero_translator_item_to_candidate(
        item,
        source="zotero_translation_server_search",
        identifier="10.1000/example",
        default_score=1.0,
        expected_title="A careful metadata pipeline",
    )

    assert candidate is not None
    assert candidate.source == "zotero_translation_server_search"
    assert candidate.fields["DOI"] == "10.1000/example"
    assert candidate.fields["abstractNote"] == "This paper tests metadata."
    assert candidate.fields["ISSN"] == "1234-5678, 8765-4321"
    assert candidate.fields["archiveLocation"] == "2401.01234"
    assert candidate.raw["publicationTitle"] == "Journal of Pipelines"


def test_title_match_score_combines_ratio_and_tokens() -> None:
    assert (
        title_match_score("A careful metadata pipeline", "A Careful Metadata Pipeline")
        == 1.0
    )
    assert (
        title_match_score("Completely different", "A Careful Metadata Pipeline") < 0.5
    )


def test_metadata_lookup_uses_shared_enricher(monkeypatch) -> None:
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace()
    processor._provider_events = []
    metadata = LocalItemMetadata(
        library_id="1",
        data_dir=Path("zotero"),
        key="PARENT1",
        item_id=1,
        version=2,
        item_type="journalArticle",
        date_modified="2026-07-15",
        fields={"title": "Example"},
        creators=[{"lastName": "Example"}],
        tags=["test"],
        collections=[{"key": "COLL1"}],
        relations=[{"dc:relation": "x"}],
    )
    attachment = LocalAttachment(
        library_id="1",
        data_dir=Path("zotero"),
        storage_dir=Path("zotero/storage"),
        key="ATTACH1",
        item_id=2,
        parent_item_id=1,
        date_modified="2026-07-15",
        link_mode=0,
        content_type="application/pdf",
        zotero_path="storage:paper.pdf",
        file_path=Path("paper.pdf"),
        parent_key="PARENT1",
    )
    expected = MetadataCandidate(
        source="arxiv",
        identifier="2101.05452",
        score=1.0,
        fields={"archive": "arXiv", "archiveLocation": "2101.05452"},
        raw={},
    )

    class FakeEnricher:
        provider_events = [{"provider": "arxiv", "status": "matched"}]

        def lookup_candidate(self, **kwargs: object) -> MetadataCandidate:
            converted_metadata = kwargs["metadata"]
            converted_attachment = kwargs["attachment"]
            assert converted_metadata.fields == metadata.fields
            assert converted_metadata.creators == metadata.creators
            assert converted_attachment.parent_key == attachment.parent_key
            return expected

    monkeypatch.setattr(processor, "_metadata_enricher", lambda: FakeEnricher())

    candidate = processor._lookup_metadata_candidate(
        metadata=metadata, attachment=attachment
    )

    assert candidate == expected
    assert processor._provider_events == [{"provider": "arxiv", "status": "matched"}]


def test_metadata_enricher_config_maps_worker_settings() -> None:
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(
        zotero_translation_server_url="http://translation-server:1969",
        zotero_translation_server_timeout_seconds=42,
        metadata_crossref_email="crossref@example.com",
        metadata_unpaywall_email="unpaywall@example.com",
        metadata_openalex_api_key="openalex-key",
        metadata_semantic_scholar_api_key="semantic-key",
        metadata_core_api_key="core-key",
        metadata_request_timeout_seconds=17,
        metadata_user_agent="test-agent",
        metadata_title_min_score=0.91,
        arxiv_search_min_score=0.92,
        metadata_policy="emptyFieldsOnly",
        metadata_extended_providers_enabled=True,
    )

    config = processor._metadata_enricher().config

    assert config.translation_server_url == "http://translation-server:1969"
    assert config.translation_server_timeout_seconds == 42
    assert config.crossref_mailto == "crossref@example.com"
    assert config.unpaywall_email == "unpaywall@example.com"
    assert config.openalex_api_key == "openalex-key"
    assert config.semantic_scholar_api_key == "semantic-key"
    assert config.core_api_key == "core-key"
    assert config.request_timeout_seconds == 17
    assert config.user_agent == "test-agent"
    assert config.metadata_title_min_score == 0.91
    assert config.arxiv_search_min_score == 0.92
    assert config.extended_providers_enabled is True


def test_metadata_drain_queue_uses_bounded_parallel_workers(monkeypatch: Any) -> None:
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(
        metadata_drain_max_workers=3,
        metadata_job_lease_seconds=60,
        metadata_policy="emptyFieldsOnly",
    )
    processor._provider_events = []

    class FakeState:
        def __init__(self) -> None:
            self.jobs = [{"job_id": f"job_{index}"} for index in range(5)]
            self.owners: list[str] = []
            self.lock = threading.Lock()

        def list_metadata_jobs(self, **_kwargs: object) -> list[dict[str, object]]:
            return list(self.jobs)

        def metadata_queue_summary(self, **_kwargs: object) -> dict[str, object]:
            return {"queued": len(self.jobs)}

        def recover_expired_metadata_jobs(self, **_kwargs: object) -> int:
            return 0

        def lease_next_metadata_job(
            self, *, owner: str, **_kwargs: object
        ) -> dict[str, object] | None:
            with self.lock:
                if not self.jobs:
                    return None
                self.owners.append(owner)
                return self.jobs.pop(0)

    fake_state = FakeState()
    processor.state = fake_state

    monkeypatch.setattr(
        ZoteroMetadataProcessor,
        "_new_drain_worker_processor",
        lambda self: processor,
    )

    def fake_drain_leased_job(
        self: ZoteroMetadataProcessor,
        *,
        job_type: str,
        job: dict[str, object],
        **_kwargs: object,
    ) -> dict[str, object]:
        assert job_type == METADATA_JOB_FULL_TEXT
        time.sleep(0.02)
        return {"job_id": job["job_id"], "status": "succeeded"}

    monkeypatch.setattr(
        ZoteroMetadataProcessor, "_drain_leased_job", fake_drain_leased_job
    )

    result = processor.drain_full_text_queue(limit=5, require_relay=False)

    assert result["processed"] == 5
    assert result["failed"] == 0
    assert result["workers"] == 3
    assert len({owner.rsplit("-", 1)[-1] for owner in fake_state.owners}) >= 2


def test_metadata_drain_heartbeats_while_handler_runs(monkeypatch: Any) -> None:
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(metadata_job_lease_seconds=60)
    heartbeats: list[dict[str, Any]] = []
    enough_heartbeats = threading.Event()

    class FakeState:
        def heartbeat_metadata_job(self, **kwargs: Any) -> bool:
            heartbeats.append(kwargs)
            if len(heartbeats) >= 3:
                enough_heartbeats.set()
            return True

        def get_metadata_job(self, _job_id: str) -> dict[str, Any]:
            return {}

    processor.state = FakeState()
    monkeypatch.setattr(
        processor,
        "_metadata_job_heartbeat_interval_seconds",
        lambda _lease_seconds: 0.01,
    )

    def slow_handler(job: dict[str, Any]) -> dict[str, Any]:
        assert job["lease_owner"] == "owner-a"
        assert enough_heartbeats.wait(timeout=2.0)
        return {"job_id": job["job_id"], "status": "succeeded"}

    monkeypatch.setattr(processor, "_drain_full_text_job", slow_handler)
    result = processor._drain_leased_job(
        job_type=METADATA_JOB_FULL_TEXT,
        job={"job_id": "job-1", "lease_owner": "owner-a", "attempts": 1},
        require_relay=False,
        policy=None,
    )

    assert result["status"] == "succeeded"
    assert len(heartbeats) >= 3
    assert all(call["job_id"] == "job-1" for call in heartbeats)
    assert all(call["owner"] == "owner-a" for call in heartbeats)
    assert all(call["lease_seconds"] == 60 for call in heartbeats)


def test_metadata_drain_binds_heartbeat_and_completion_to_attempt(
    monkeypatch: Any,
) -> None:
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(metadata_job_lease_seconds=60)
    heartbeats: list[dict[str, Any]] = []
    completions: list[dict[str, Any]] = []

    class FakeState:
        def heartbeat_metadata_job(self, **kwargs: Any) -> bool:
            heartbeats.append(kwargs)
            return True

        def mark_metadata_job_succeeded(self, **kwargs: Any) -> dict[str, Any]:
            completions.append(kwargs)
            return {
                "job_id": kwargs["job_id"],
                "status": "succeeded",
                "attempts": kwargs["attempt"],
            }

    processor.state = FakeState()
    monkeypatch.setattr(
        processor,
        "_metadata_job_heartbeat_interval_seconds",
        lambda _lease_seconds: 60.0,
    )

    def handler(job: dict[str, Any]) -> dict[str, Any]:
        return processor._mark_metadata_job_succeeded(
            job,
            job_id=str(job["job_id"]),
            message="done",
        )

    monkeypatch.setattr(processor, "_drain_full_text_job", handler)
    result = processor._drain_leased_job(
        job_type=METADATA_JOB_FULL_TEXT,
        job={
            "job_id": "meta-attempt",
            "lease_owner": "owner-a",
            "attempts": 3,
        },
        require_relay=False,
        policy=None,
    )

    assert result["status"] == "succeeded"
    assert heartbeats[0]["attempt"] == 3
    assert completions[0]["attempt"] == 3


def test_metadata_drain_rejects_stale_lease_before_handler(monkeypatch: Any) -> None:
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(metadata_job_lease_seconds=60)

    class FakeState:
        def heartbeat_metadata_job(self, **_kwargs: Any) -> bool:
            return False

        def get_metadata_job(self, job_id: str) -> dict[str, Any]:
            return {
                "job_id": job_id,
                "status": "running",
                "lease_owner": "new-owner",
            }

    processor.state = FakeState()

    def stale_handler(_job: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("stale metadata handler must not execute")

    monkeypatch.setattr(processor, "_drain_full_text_job", stale_handler)
    result = processor._drain_leased_job(
        job_type=METADATA_JOB_FULL_TEXT,
        job={"job_id": "job-1", "lease_owner": "old-owner", "attempts": 1},
        require_relay=False,
        policy=None,
    )

    assert result["status"] == "running"
    assert result["stale_lease"] is True
    assert result["job"]["lease_owner"] == "new-owner"


def test_relay_url_candidates_add_localhost_for_compose_service_name() -> None:
    assert _relay_url_candidates(
        "http://zotero-file-relay:23119",
        "/attachments/PDF1234/parent/metadata",
    ) == [
        "http://zotero-file-relay:23119/attachments/PDF1234/parent/metadata",
        "http://127.0.0.1:23119/attachments/PDF1234/parent/metadata",
    ]
    assert _relay_url_candidates("http://127.0.0.1:23119", "/health") == [
        "http://127.0.0.1:23119/health",
    ]


def test_web_api_config_errors_are_not_retryable() -> None:
    assert _is_nonretryable_worker_error(
        RuntimeError(
            "zotero-file-relay metadata patch failed: "
            "{'ok': False, 'error': 'WEB_API_NOT_CONFIGURED'}"
        )
    )
    assert not _is_nonretryable_worker_error(
        RuntimeError("temporary connection refused")
    )


def test_library_binding_errors_are_skipped_not_failed() -> None:
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    captured: dict[str, Any] = {}

    class FakeState:
        def mark_metadata_job_skipped(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"status": "skipped", **kwargs}

        def mark_metadata_job_failed(self, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError(f"metadata job should not fail: {kwargs}")

    processor.state = FakeState()

    result = processor._mark_metadata_job_failed_or_skipped_for_exception(
        job_id="job1",
        exc=RuntimeError("zotero-file-relay failed: LIBRARY_BINDING_NOT_CONFIGURED"),
    )

    assert result["status"] == "skipped"
    assert captured["result"]["reason"] == "library_binding_not_configured"


def test_deleted_local_attachment_errors_are_skipped_not_failed() -> None:
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    captured: dict[str, Any] = {}

    class FakeState:
        def mark_metadata_job_skipped(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"status": "skipped", **kwargs}

        def mark_metadata_job_failed(self, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError(f"metadata job should not fail: {kwargs}")

    processor.state = FakeState()

    result = processor._mark_metadata_job_failed_or_skipped_for_exception(
        job_id="job1",
        exc=FileNotFoundError(
            "Local Zotero PDF attachment is deleted or inactive: PDF1234"
        ),
    )

    assert result["status"] == "skipped"
    assert captured["result"]["reason"] == "local_attachment_deleted_or_inactive"


def test_cloudflare_metadata_403_is_nonactionable() -> None:
    assert (
        _nonactionable_metadata_http_error_reason(
            403,
            "<html><head><title>Just a moment...</title></head><body>Cloudflare</body></html>",
        )
        == "metadata_provider_blocked"
    )
    assert _nonactionable_metadata_http_error_reason(500, "Cloudflare") is None


def test_metadata_patch_relay_payload_includes_library_id(monkeypatch: Any) -> None:
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(zotero_relay_url="http://relay")
    captured: dict[str, object] = {}

    def fake_request_json(
        self: ZoteroRelayClient, **kwargs: object
    ) -> dict[str, object]:
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(ZoteroRelayClient, "request_json", fake_request_json)
    attachment = SimpleNamespace(
        key="PDF1234",
        library_id="Zotero_Test_Data_abcd1234",
        state_key="Zotero_Test_Data_abcd1234_PDF1234",
    )
    metadata = SimpleNamespace(key="ITEM1234", version=7)

    result = processor._patch_parent_metadata_via_relay(
        attachment=attachment,
        metadata=metadata,
        fields={"archive": "arXiv"},
        policy="emptyFieldsOnly",
    )

    assert result == {"ok": True}
    assert captured["path"] == "/attachments/PDF1234/parent/metadata"
    assert captured["payload"]["libraryId"] == "Zotero_Test_Data_abcd1234"  # type: ignore[index]
    assert captured["payload"]["expectedVersion"] == 0  # type: ignore[index]
    assert "refresh:" in captured["payload"]["deduplicationKey"]  # type: ignore[index]


def test_html_sibling_local_copy_reuses_bounded_parent_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    source.write_text("<html><body>Article</body></html>", encoding="utf-8")
    attachment = LocalAttachment(
        library_id="LIB1",
        data_dir=tmp_path,
        storage_dir=tmp_path / "storage",
        key="PDF1234",
        item_id=20,
        parent_item_id=10,
        date_modified=None,
        link_mode=0,
        content_type="application/pdf",
        zotero_path="storage:paper.pdf",
        file_path=tmp_path / "paper.pdf",
        parent_key="ITEM1234",
    )
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)

    def reject_copy2(*_args: object, **_kwargs: object) -> None:
        raise OSError("unbounded shutil.copy2 was used")

    monkeypatch.setattr(shutil, "copy2", reject_copy2)

    result = processor._write_html_sibling_local_copy(
        attachment=attachment,
        source_path=source,
        filename="Article [ARXIV HTML].html",
        relay_result={"ok": True, "siblingKey": "HTML1234"},
    )

    assert result["ok"] is True
    assert result["siblingKey"] == "HTML1234"
    assert Path(result["path"]).read_bytes() == source.read_bytes()


@pytest.mark.parametrize(
    "relay_result",
    [
        {"ok": "true", "siblingKey": "HTML1234"},
        {"ok": True, "siblingKey": 1234},
    ],
    ids=["malformed-ok", "numeric-key"],
)
def test_html_sibling_local_copy_rejects_malformed_relay_contract(
    tmp_path: Path,
    relay_result: dict[str, object],
) -> None:
    source = tmp_path / "article.html"
    source.write_text("<html><body>Article</body></html>", encoding="utf-8")
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)

    with pytest.raises(RuntimeError):
        processor._write_html_sibling_local_copy(
            attachment=attachment,
            source_path=source,
            filename="Article [ARXIV HTML].html",
            relay_result=relay_result,
        )


def test_relay_attachment_key_adapter_rejects_numeric_key() -> None:
    relay_result: dict[str, object] = {"ok": True, "siblingKey": 1234}

    adapted = metadata_processor_module._relay_result_with_attachment_key(
        relay_result  # type: ignore[arg-type]
    )

    assert adapted == relay_result
    assert "newAttachmentKey" not in adapted


def test_metadata_drain_ensures_parent_for_standalone_pdf(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF")
    attachment = LocalAttachment(
        library_id="LIB1",
        data_dir=tmp_path,
        storage_dir=tmp_path,
        key="PDF1234",
        item_id=20,
        parent_item_id=None,
        date_modified=None,
        link_mode=0,
        content_type="application/pdf",
        zotero_path="storage:paper.pdf",
        file_path=pdf,
    )
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(
        zotero_relay_url="http://relay",
        zotero_parent_preflight_enabled=True,
        metadata_policy="emptyFieldsOnly",
    )
    processor._provider_events = []
    captured: dict[str, Any] = {}

    class FakeState:
        def mark_metadata_job_succeeded(self, **kwargs: Any) -> dict[str, Any]:
            captured["succeeded"] = kwargs
            return {"status": "succeeded", **kwargs}

        def mark_metadata_job_skipped(self, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError(f"metadata job should not be skipped: {kwargs}")

        def mark_metadata_job_failed(self, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError(f"metadata job should not fail: {kwargs}")

    class FakeZotero:
        def get_parent_metadata_for_attachment(
            self,
            checked: LocalAttachment,
        ) -> None:
            assert checked.key == "PDF1234"
            return None

    processor.state = FakeState()
    monkeypatch.setattr(processor, "_attachment_for_job", lambda _job: attachment)
    monkeypatch.setattr(processor, "_config_for_job", lambda _job: processor.config)
    monkeypatch.setattr(
        "zotero_ingest_worker.metadata_processor.LocalZoteroStore",
        lambda _config: FakeZotero(),
    )
    monkeypatch.setattr(
        processor,
        "_ensure_parent_via_relay",
        lambda checked: {
            "ok": True,
            "parentItemKey": "ITEM1234",
            "alreadyHadParent": False,
            "parentCreated": {
                "key": "ITEM1234",
                "title": "paper",
                "itemType": "document",
                "version": 9,
                "collections": [],
            },
            "pdfParentPatch": {
                "ok": True,
                "pdfKey": checked.key,
                "parentItemKey": "ITEM1234",
                "oldVersion": 7,
                "newVersion": 8,
                "clearedCollections": False,
            },
        },
    )
    monkeypatch.setattr(
        processor,
        "_lookup_metadata_candidate",
        lambda **_kwargs: MetadataCandidate(
            source="crossref",
            identifier="10.1000/example",
            score=1.0,
            fields={"title": "Better Paper", "DOI": "10.1000/example"},
            raw={},
        ),
    )

    def fake_patch_parent_metadata_via_relay(**kwargs: Any) -> dict[str, Any]:
        captured["patch_call"] = kwargs
        return {"ok": True, "appliedFields": ["DOI"], "newVersion": 10}

    monkeypatch.setattr(
        processor,
        "_patch_parent_metadata_via_relay",
        fake_patch_parent_metadata_via_relay,
    )

    result = processor._drain_enrich_job(
        {
            "job_id": "job1",
            "attachment_key": "PDF1234",
            "data_dir": str(tmp_path),
            "source_path": str(pdf),
            "source_size": pdf.stat().st_size,
            "source_mtime_ns": pdf.stat().st_mtime_ns,
        },
        require_relay=True,
        policy="emptyFieldsOnly",
    )

    assert result["status"] == "succeeded"
    patch_call = captured["patch_call"]
    assert patch_call["attachment"].parent_key == "ITEM1234"
    assert patch_call["metadata"].key == "ITEM1234"
    assert patch_call["fields"] == {"DOI": "10.1000/example"}
    stored = captured["succeeded"]["result"]
    assert stored["parent_item_key"] == "ITEM1234"
    assert stored["parent_preflight"]["parentCreated"]["key"] == "ITEM1234"
    assert stored["local_metadata"]["reason"] == "parent_not_in_local_sqlite"


@pytest.mark.parametrize(
    "relay_result",
    [
        {"ok": "true", "parentItemKey": "ITEMNEW1"},
        {"ok": True, "parentItemKey": 12345678},
    ],
    ids=["malformed-ok", "numeric-parent-key"],
)
def test_parent_preflight_rejects_malformed_contract_before_local_sync(
    monkeypatch: Any,
    tmp_path: Path,
    relay_result: dict[str, Any],
) -> None:
    config = _metadata_processor_test_config(tmp_path, tmp_path)
    config.zotero_relay_url = "http://relay"
    config.zotero_parent_preflight_enabled = True
    processor = ZoteroMetadataProcessor(config)
    attachment = LocalAttachment(
        library_id="LOCAL",
        data_dir=tmp_path,
        storage_dir=tmp_path / "storage",
        key="PDF1234",
        item_id=20,
        parent_item_id=None,
        date_modified=None,
        link_mode=0,
        content_type="application/pdf",
        zotero_path="storage:paper.pdf",
        file_path=tmp_path / "storage" / "PDF1234" / "paper.pdf",
        parent_key=None,
    )
    zotero = SimpleNamespace(
        get_parent_metadata_for_attachment=lambda _attachment: None,
    )
    monkeypatch.setattr(
        processor,
        "_ensure_parent_via_relay",
        lambda _attachment: relay_result,
    )

    def forbid_local_sync(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("malformed parent preflight must not mutate local state")

    monkeypatch.setattr(processor, "_sync_ensured_parent_locally", forbid_local_sync)

    updated_attachment, metadata, preflight = processor._ensure_parent_metadata_context(
        zotero=zotero,
        attachment=attachment,
    )

    assert updated_attachment is attachment
    assert metadata is None
    assert preflight is relay_result


def test_parent_preflight_does_not_synthesize_after_local_sync_rejects_contract(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    config = _metadata_processor_test_config(tmp_path, tmp_path)
    config.zotero_relay_url = "http://relay"
    config.zotero_parent_preflight_enabled = True
    processor = ZoteroMetadataProcessor(config)
    attachment = LocalAttachment(
        library_id="LOCAL",
        data_dir=tmp_path,
        storage_dir=tmp_path / "storage",
        key="PDF1234",
        item_id=20,
        parent_item_id=None,
        date_modified=None,
        link_mode=0,
        content_type="application/pdf",
        zotero_path="storage:paper.pdf",
        file_path=tmp_path / "storage" / "PDF1234" / "paper.pdf",
        parent_key=None,
    )
    zotero = SimpleNamespace(
        get_parent_metadata_for_attachment=lambda _attachment: None,
    )
    relay_result = {"ok": True, "parentItemKey": "ITEMNEW1"}
    monkeypatch.setattr(
        processor,
        "_ensure_parent_via_relay",
        lambda _attachment: relay_result,
    )
    monkeypatch.setattr(
        processor,
        "_sync_ensured_parent_locally",
        lambda **_kwargs: {
            **relay_result,
            "local_sync": {
                "ok": False,
                "reason": "invalid_parent_preflight_contract",
                "field": "pdfParentPatch.ok",
            },
        },
    )

    updated_attachment, metadata, preflight = processor._ensure_parent_metadata_context(
        zotero=zotero,
        attachment=attachment,
    )

    assert updated_attachment is attachment
    assert metadata is None
    assert preflight is not None
    assert preflight["local_sync"]["ok"] is False


def test_parent_preflight_syncs_standalone_pdf_to_local_sqlite(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    data_dir = _write_full_text_fixture(tmp_path, with_pdf=False)
    (data_dir / "storage" / "PDF1234").mkdir()
    (data_dir / "storage" / "PDF1234" / "paper.pdf").write_bytes(b"%PDF")
    connection = sqlite3.connect(data_dir / "zotero.sqlite")
    try:
        connection.executescript(
            """
            insert into itemTypes values (3, 'document');
            insert into items values (20, 2, '2026-01-02', 'PDF1234', 1);
            insert into itemAttachments values (20, null, 0, 'application/pdf', 'storage:paper.pdf');
            """
        )
        connection.commit()
    finally:
        connection.close()

    config = _metadata_processor_test_config(tmp_path, data_dir)
    config.zotero_relay_url = "http://relay"
    processor = ZoteroMetadataProcessor(config)
    zotero = LocalZoteroStore(config)
    attachment = zotero.get_attachment("PDF1234")

    assert attachment.parent_key is None
    monkeypatch.setattr(
        processor,
        "_ensure_parent_via_relay",
        lambda checked: {
            "ok": True,
            "parentItemKey": "ITEMNEW1",
            "parentCreated": {
                "key": "ITEMNEW1",
                "title": "Standalone paper",
                "itemType": "document",
                "version": 11,
                "collections": [],
            },
            "pdfParentPatch": {
                "ok": True,
                "pdfKey": checked.key,
                "parentItemKey": "ITEMNEW1",
                "oldVersion": 1,
                "newVersion": 12,
                "clearedCollections": False,
            },
            "alreadyHadParent": False,
        },
    )

    updated_attachment, metadata, preflight = processor._ensure_parent_metadata_context(
        zotero=zotero,
        attachment=attachment,
    )

    assert metadata is not None
    assert metadata.key == "ITEMNEW1"
    assert metadata.item_id > 0
    assert metadata.item_type == "document"
    assert metadata.title == "Standalone paper"
    assert updated_attachment.parent_key == "ITEMNEW1"
    assert updated_attachment.parent_item_id == metadata.item_id
    assert preflight["local_sync"]["parentInserted"] is True

    refreshed = zotero.get_attachment("PDF1234")
    assert refreshed.parent_key == "ITEMNEW1"
    assert refreshed.parent_item_id == metadata.item_id


def test_local_zotero_get_attachment_uses_keyed_sqlite_lookup(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    data_dir = _write_full_text_fixture(tmp_path, with_pdf=False)
    (data_dir / "storage" / "PDF1234").mkdir()
    pdf = data_dir / "storage" / "PDF1234" / "paper.pdf"
    pdf.write_bytes(b"%PDF")
    connection = sqlite3.connect(data_dir / "zotero.sqlite")
    try:
        connection.executescript(
            """
            insert into items values (20, 2, '2026-01-02', 'PDF1234', 1);
            insert into itemAttachments values (20, null, 0, 'application/pdf', 'storage:paper.pdf');
            """
        )
        connection.commit()
    finally:
        connection.close()

    store = LocalZoteroStore(_metadata_processor_test_config(tmp_path, data_dir))

    def forbid_full_scan(**_kwargs: object) -> None:
        raise AssertionError("full attachment scan should not run")

    monkeypatch.setattr(store, "_iter_sqlite_pdf_attachments", forbid_full_scan)

    attachment = store.get_attachment("PDF1234")

    assert attachment.key == "PDF1234"
    assert attachment.file_path == pdf
    assert attachment.filename == "paper.pdf"


def test_parent_preflight_syncs_storage_only_pdf_to_local_sqlite(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    data_dir = _write_full_text_fixture(tmp_path, with_pdf=False)
    connection = sqlite3.connect(data_dir / "zotero.sqlite")
    try:
        connection.execute("insert into itemTypes values (3, 'document')")
        connection.commit()
    finally:
        connection.close()
    storage_dir = data_dir / "storage" / "PDFONLY1"
    storage_dir.mkdir()
    pdf = storage_dir / "paper.pdf"
    pdf.write_bytes(b"%PDF")

    config = _metadata_processor_test_config(tmp_path, data_dir)
    config.zotero_relay_url = "http://relay"
    processor = ZoteroMetadataProcessor(config)
    zotero = LocalZoteroStore(config)
    attachment = LocalAttachment(
        library_id=zotero.library_id,
        data_dir=data_dir,
        storage_dir=data_dir / "storage",
        key="PDFONLY1",
        item_id=None,
        parent_item_id=None,
        date_modified=None,
        link_mode=None,
        content_type="application/pdf",
        zotero_path="storage:paper.pdf",
        file_path=pdf,
    )
    monkeypatch.setattr(
        processor,
        "_ensure_parent_via_relay",
        lambda checked: {
            "ok": True,
            "parentItemKey": "ITEMNEW2",
            "parentCreated": {
                "key": "ITEMNEW2",
                "title": "Storage-only paper",
                "itemType": "document",
                "version": 21,
                "collections": [],
            },
            "pdfParentPatch": {
                "ok": True,
                "pdfKey": checked.key,
                "parentItemKey": "ITEMNEW2",
                "oldVersion": 1,
                "newVersion": 22,
                "clearedCollections": False,
            },
            "alreadyHadParent": False,
        },
    )

    updated_attachment, metadata, preflight = processor._ensure_parent_metadata_context(
        zotero=zotero,
        attachment=attachment,
    )

    assert metadata is not None
    assert metadata.key == "ITEMNEW2"
    assert updated_attachment.parent_item_id == metadata.item_id
    assert preflight["local_sync"]["attachmentInserted"] is True

    refreshed = zotero.get_attachment("PDFONLY1")
    assert refreshed.item_id is not None
    assert refreshed.parent_key == "ITEMNEW2"
    assert refreshed.parent_item_id == metadata.item_id


def test_arxiv_html_filename_uses_distinct_suffix() -> None:
    assert arxiv_html_filename("paper.pdf") == "paper [ARXIV HTML].html"
    assert arxiv_html_filename("paper [ARXIV HTML].pdf") == "paper [ARXIV HTML].html"


def test_validate_arxiv_html_rejects_non_html_and_short_pages() -> None:
    valid = "<html><body>" + ("This is article text. " * 20) + "</body></html>"

    assert validate_arxiv_html(valid, min_text_chars=100)["ok"] is True
    assert (
        validate_arxiv_html("not html", min_text_chars=1)["reason"]
        == "missing_html_tag"
    )
    assert (
        validate_arxiv_html("<html><body>tiny</body></html>", min_text_chars=100)[
            "reason"
        ]
        == "too_little_text"
    )


def test_arxiv_validation_failure_is_skipped_not_failed(
    monkeypatch: Any, tmp_path: Path
) -> None:
    data_dir = _write_full_text_fixture(tmp_path, with_pdf=True)
    config = _metadata_processor_test_config(tmp_path, data_dir)
    processor = ZoteroMetadataProcessor(config)
    processor._library_configs = lambda **_kwargs: [config]  # type: ignore[method-assign]
    pdf_path = data_dir / "storage" / "PDF1234" / "paper.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF")

    processor.state.enqueue_metadata_job(
        job_type="arxiv_html",
        queue_key="arxiv_html|PDF1234",
        library_id="LOCAL",
        attachment_key="PDF1234",
        data_dir=data_dir,
        source_path=pdf_path,
        signature=FileSignature.from_path(pdf_path),
        status="queued",
        reason="test",
    )
    monkeypatch.setattr(
        "zotero_ingest_worker.metadata_processor.ArxivHtmlJobService.lookup_candidate",
        lambda self, **_kwargs: MetadataCandidate(
            source="arxiv",
            identifier="2401.01234",
            score=1.0,
            fields={"title": "Example"},
            raw={},
        ),
    )
    monkeypatch.setattr(
        "zotero_ingest_worker.metadata_processor.ArxivHtmlJobService.fetch_html",
        lambda self, arxiv_id: (_ for _ in ()).throw(
            ArxivHtmlValidationError(arxiv_id=arxiv_id, reason="too_little_text")
        ),
    )

    result = processor.drain_arxiv_html_queue(limit=1, require_relay=False)

    assert result["ok"] is True
    assert result["failed"] == 0
    assert result["results"][0]["status"] == "skipped"
    stored = json.loads(result["results"][0]["result_json"])
    assert stored["reason"] == "arxiv_html_validation_failed"
    assert stored["validation_reason"] == "too_little_text"


def test_arxiv_attach_without_parent_is_skipped_before_local_sync(
    tmp_path: Path,
) -> None:
    data_dir = _write_full_text_fixture(tmp_path, with_pdf=True)
    connection = sqlite3.connect(data_dir / "zotero.sqlite")
    try:
        connection.execute(
            "update itemAttachments set parentItemID = null where itemID = 20",
        )
        connection.commit()
    finally:
        connection.close()

    config = _metadata_processor_test_config(tmp_path, data_dir)
    config.arxiv_html_attach = True
    config.zotero_relay_url = ""
    processor = ZoteroMetadataProcessor(config)
    processor._library_configs = lambda **_kwargs: [config]  # type: ignore[method-assign]
    pdf_path = data_dir / "storage" / "PDF1234" / "paper.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF")
    processor.state.enqueue_metadata_job(
        job_type="arxiv_html",
        queue_key="arxiv_html|PDF1234",
        library_id="LOCAL",
        attachment_key="PDF1234",
        data_dir=data_dir,
        source_path=pdf_path,
        signature=FileSignature.from_path(pdf_path),
        status="queued",
        reason="test",
    )

    result = processor.drain_arxiv_html_queue(limit=1, require_relay=False)

    assert result["ok"] is True
    assert result["failed"] == 0
    assert result["results"][0]["status"] == "skipped"
    stored = json.loads(result["results"][0]["result_json"])
    assert stored["reason"] == "no_parent_item"
    assert stored["parent_preflight"]["reason"] == "relay_not_configured"


def test_local_zotero_reads_extended_parent_metadata(tmp_path: Path) -> None:
    data_dir = tmp_path / "Zotero_Test_Data"
    storage_dir = data_dir / "storage" / "PDF1234"
    storage_dir.mkdir(parents=True)
    pdf_path = storage_dir / "paper.pdf"
    pdf_path.write_bytes(b"%PDF")
    sqlite_path = data_dir / "zotero.sqlite"

    connection = sqlite3.connect(sqlite_path)
    try:
        connection.executescript(
            """
            create table itemTypes (itemTypeID integer primary key, typeName text);
            create table items (
                itemID integer primary key,
                itemTypeID integer,
                dateModified text,
                key text,
                version integer
            );
            create table deletedItems (itemID integer primary key);
            create table itemAttachments (
                itemID integer primary key,
                parentItemID integer,
                linkMode integer,
                contentType text,
                path text
            );
            create table fields (fieldID integer primary key, fieldName text);
            create table itemDataValues (valueID integer primary key, value text);
            create table itemData (itemID integer, fieldID integer, valueID integer);
            create table creatorTypes (creatorTypeID integer primary key, creatorType text);
            create table creators (creatorID integer primary key, firstName text, lastName text, fieldMode integer);
            create table itemCreators (itemID integer, creatorID integer, creatorTypeID integer, orderIndex integer);
            create table tags (tagID integer primary key, name text);
            create table itemTags (itemID integer, tagID integer);
            create table collections (collectionID integer primary key, key text, collectionName text);
            create table collectionItems (collectionID integer, itemID integer);
            create table relations (subject integer, predicate text, object text);
            insert into itemTypes values (1, 'journalArticle'), (2, 'attachment');
            insert into items values (10, 1, '2026-01-01', 'PARENT1', 42);
            insert into items values (20, 2, '2026-01-02', 'PDF1234', 7);
            insert into itemAttachments values (20, 10, 0, 'application/pdf', 'storage:paper.pdf');
            insert into fields values (1, 'title'), (2, 'DOI'), (3, 'extra');
            insert into itemDataValues values
              (1, 'A Careful Metadata Pipeline'),
              (2, '10.48550/arXiv.2401.01234'),
              (3, 'arXiv:2401.01234 [cs.DL]');
            insert into itemData values (10, 1, 1), (10, 2, 2), (10, 3, 3);
            insert into creatorTypes values (1, 'author');
            insert into creators values (1, 'Ada', 'Lovelace', 0);
            insert into itemCreators values (10, 1, 1, 0);
            insert into tags values (1, 'Digital Libraries');
            insert into itemTags values (10, 1);
            insert into collections values (1, 'COLL1', 'Meine');
            insert into collectionItems values (1, 10);
            insert into relations values (10, 'dc:relation', 'https://arxiv.org/abs/2401.01234');
            """
        )
        connection.commit()
    finally:
        connection.close()

    config = SimpleNamespace(
        zotero_data_dir=data_dir,
        zotero_sqlite_path=sqlite_path,
        resolved_storage_dir=data_dir / "storage",
    )
    attachment = LocalZoteroStore(config).get_attachment("PDF1234")
    metadata = LocalZoteroStore(config).get_parent_metadata_for_attachment(attachment)

    assert metadata is not None
    assert metadata.key == "PARENT1"
    assert metadata.version == 42
    assert metadata.title == "A Careful Metadata Pipeline"
    assert metadata.creators[0]["lastName"] == "Lovelace"
    assert metadata.tags == ["Digital Libraries"]
    assert metadata.collections[0]["name"] == "Meine"
    assert metadata.relations[0]["object"] == "https://arxiv.org/abs/2401.01234"


def test_local_zotero_treats_deleted_parent_metadata_as_missing(tmp_path: Path) -> None:
    data_dir = tmp_path / "Zotero_Test_Data"
    storage_dir = data_dir / "storage" / "PDF1234"
    storage_dir.mkdir(parents=True)
    (storage_dir / "paper.pdf").write_bytes(b"%PDF")
    sqlite_path = data_dir / "zotero.sqlite"

    connection = sqlite3.connect(sqlite_path)
    try:
        connection.executescript(
            """
            create table itemTypes (itemTypeID integer primary key, typeName text);
            create table items (
                itemID integer primary key,
                itemTypeID integer,
                dateModified text,
                key text,
                version integer
            );
            create table deletedItems (itemID integer primary key);
            create table itemAttachments (
                itemID integer primary key,
                parentItemID integer,
                linkMode integer,
                contentType text,
                path text
            );
            create table fields (fieldID integer primary key, fieldName text);
            create table itemDataValues (valueID integer primary key, value text);
            create table itemData (itemID integer, fieldID integer, valueID integer);
            create table creatorTypes (creatorTypeID integer primary key, creatorType text);
            create table creators (creatorID integer primary key, firstName text, lastName text, fieldMode integer);
            create table itemCreators (itemID integer, creatorID integer, creatorTypeID integer, orderIndex integer);
            create table tags (tagID integer primary key, name text);
            create table itemTags (itemID integer, tagID integer);
            create table collections (collectionID integer primary key, key text, collectionName text);
            create table collectionItems (collectionID integer, itemID integer);
            create table relations (subject integer, predicate text, object text);
            insert into itemTypes values (1, 'journalArticle'), (2, 'attachment');
            insert into items values (10, 1, '2026-01-01', 'PARENT1', 42);
            insert into items values (20, 2, '2026-01-02', 'PDF1234', 7);
            insert into deletedItems values (10);
            insert into itemAttachments values (20, 10, 0, 'application/pdf', 'storage:paper.pdf');
            """
        )
        connection.commit()
    finally:
        connection.close()

    config = SimpleNamespace(
        zotero_data_dir=data_dir,
        zotero_sqlite_path=sqlite_path,
        resolved_storage_dir=data_dir / "storage",
    )
    store = LocalZoteroStore(config)
    attachment = store.get_attachment("PDF1234")

    assert attachment.parent_key == "PARENT1"
    assert store.get_parent_metadata_for_attachment(attachment) is None


def test_metadata_state_queue_lifecycle(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF")
    store = OcrStateStore(tmp_path / "state.sqlite")
    signature = FileSignature.from_path(source)

    job = store.enqueue_metadata_job(
        job_type="enrich",
        library_id="LIB1",
        attachment_key="PDF1234",
        data_dir=tmp_path,
        source_path=source,
        signature=signature,
        status="queued",
        reason="test",
    )
    duplicate = store.enqueue_metadata_job(
        job_type="enrich",
        library_id="LIB1",
        attachment_key="PDF1234",
        data_dir=tmp_path,
        source_path=source,
        signature=signature,
        status="queued",
        reason="test",
    )

    assert job["created"] is True
    assert duplicate["created"] is False
    assert store.metadata_queue_summary(job_type="enrich")["queued"] == 1

    leased = store.lease_next_metadata_job(
        job_type="enrich", owner="test", lease_seconds=60
    )
    assert leased is not None
    assert leased["status"] == "running"

    done = store.mark_metadata_job_succeeded(
        job_id=leased["job_id"],
        message="done",
        result={"ok": True},
    )

    assert done["status"] == "succeeded"
    assert store.metadata_queue_summary(job_type="enrich")["succeeded"] == 1


def test_metadata_retry_can_reset_exhausted_attempts(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF")
    store = OcrStateStore(tmp_path / "state.sqlite")
    signature = FileSignature.from_path(source)

    keep_attempts = store.enqueue_metadata_job(
        job_type="arxiv_html",
        library_id="LIB1",
        attachment_key="PDF1234",
        data_dir=tmp_path,
        source_path=source,
        signature=signature,
        status="queued",
        reason="test",
        queue_key="keep",
        max_attempts=1,
    )
    leased = store.lease_next_metadata_job(
        job_type="arxiv_html", owner="test", lease_seconds=60
    )
    assert leased is not None
    failed = store.mark_metadata_job_failed(
        job_id=str(leased["job_id"]),
        message="timed out",
        retryable=True,
    )
    assert failed["status"] == "failed_final"
    retried = store.retry_metadata_job(str(keep_attempts["job_id"]))
    assert retried["status"] == "failed_final"
    assert retried["attempts"] == 1
    assert (
        store.lease_next_metadata_job(
            job_type="arxiv_html", owner="test", lease_seconds=60
        )
        is None
    )
    with store._connect() as connection:
        events = connection.execute(
            "select event from metadata_job_events where job_id = ? order by event_id",
            (keep_attempts["job_id"],),
        ).fetchall()
    assert "retry_rejected_exhausted" in {str(row["event"]) for row in events}

    reset_attempts = store.enqueue_metadata_job(
        job_type="arxiv_html",
        library_id="LIB1",
        attachment_key="PDF5678",
        data_dir=tmp_path,
        source_path=source,
        signature=signature,
        status="queued",
        reason="test",
        queue_key="reset",
        max_attempts=1,
    )
    leased = store.lease_next_metadata_job(
        job_type="arxiv_html", owner="test", lease_seconds=60
    )
    assert leased is not None
    failed = store.mark_metadata_job_failed(
        job_id=str(leased["job_id"]),
        message="timed out",
        retryable=True,
    )
    assert failed["status"] == "failed_final"
    retried = store.retry_metadata_job(
        str(reset_attempts["job_id"]), reset_attempts=True
    )
    assert retried["status"] == "queued"
    assert retried["attempts"] == 0
    leased = store.lease_next_metadata_job(
        job_type="arxiv_html", owner="new-generation", lease_seconds=60
    )
    assert leased is not None
    assert leased["job_id"] == reset_attempts["job_id"]
    assert leased["attempts"] == 1


def test_metadata_claim_finalizes_exhausted_queued_job(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF")
    store = OcrStateStore(tmp_path / "state.sqlite")
    created = store.enqueue_metadata_job(
        job_type="full_text",
        library_id="LIB1",
        attachment_key="PDF1",
        data_dir=tmp_path,
        source_path=source,
        signature=FileSignature.from_path(source),
        status="queued",
        reason="test",
        max_attempts=1,
    )
    with store._connect() as connection:
        connection.execute(
            "update metadata_jobs set attempts = max_attempts where job_id = ?",
            (created["job_id"],),
        )

    leased = store.lease_next_metadata_job(
        job_type="full_text", owner="worker", lease_seconds=60
    )
    finalized = store.get_metadata_job(str(created["job_id"]))

    assert leased is None
    assert finalized is not None
    assert finalized["status"] == "failed_final"
    assert finalized["last_error"] == "Attempt budget exhausted before claim."


def test_metadata_zero_max_attempts_is_unlimited(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF")
    store = OcrStateStore(tmp_path / "state.sqlite")
    created = store.enqueue_metadata_job(
        job_type="full_text",
        library_id="LIB1",
        attachment_key="PDF1",
        data_dir=tmp_path,
        source_path=source,
        signature=FileSignature.from_path(source),
        status="queued",
        reason="test",
        max_attempts=0,
    )

    first = store.lease_next_metadata_job(
        job_type="full_text", owner="worker-1", lease_seconds=60
    )
    assert first is not None
    failed = store.mark_metadata_job_failed(
        job_id=str(created["job_id"]),
        message="transient",
        retryable=True,
        owner="worker-1",
    )
    assert failed["status"] == "failed_retryable"
    retried = store.retry_metadata_job(str(created["job_id"]))
    assert retried["status"] == "queued"
    second = store.lease_next_metadata_job(
        job_type="full_text", owner="worker-2", lease_seconds=60
    )
    assert second is not None
    assert second["attempts"] == 2


def test_metadata_state_parent_scope_dedupes_changed_container_signature(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "zotero.sqlite"
    sqlite_path.write_bytes(b"first")
    store = OcrStateStore(tmp_path / "state.sqlite")
    first_signature = FileSignature.from_path(sqlite_path)
    first = store.enqueue_metadata_job(
        job_type="full_text",
        library_id="LIB1",
        attachment_key="PARENT1",
        data_dir=tmp_path,
        source_path=sqlite_path,
        signature=first_signature,
        status="queued",
        reason="test",
        parent_item_key="PARENT1",
        parent_version=12,
        queue_key="full-text-v1",
    )
    sqlite_path.write_bytes(b"second container state")
    second_signature = FileSignature.from_path(sqlite_path)
    second = store.enqueue_metadata_job(
        job_type="full_text",
        library_id="LIB1",
        attachment_key="PARENT1",
        data_dir=tmp_path,
        source_path=sqlite_path,
        signature=second_signature,
        status="queued",
        reason="test",
        parent_item_key="PARENT1",
        parent_version=12,
        queue_key="full-text-v1",
    )

    assert first["created"] is True
    assert second["created"] is True
    scoped = store.get_metadata_job_by_parent_scope(
        job_type="full_text",
        library_id="LIB1",
        parent_item_key="PARENT1",
        parent_version=12,
        queue_key="full-text-v1",
        statuses={"queued"},
    )

    assert scoped is not None
    assert scoped["job_id"] == second["job_id"]


def test_metadata_backlog_scan_dedupes_multiple_pdfs_for_same_parent(
    tmp_path: Path,
) -> None:
    data_dir = _write_full_text_fixture(tmp_path, with_pdf=True)
    (data_dir / "storage" / "PDF1234").mkdir()
    (data_dir / "storage" / "PDF1234" / "paper.pdf").write_bytes(b"%PDF-1.4 first")
    (data_dir / "storage" / "PDF5678").mkdir()
    (data_dir / "storage" / "PDF5678" / "second.pdf").write_bytes(b"%PDF-1.4 second")
    connection = sqlite3.connect(data_dir / "zotero.sqlite")
    try:
        connection.executescript(
            """
            insert into items values (21, 2, '2026-01-03', 'PDF5678', 1);
            insert into itemAttachments values (21, 10, 0, 'application/pdf', 'storage:second.pdf');
            """
        )
        connection.commit()
    finally:
        connection.close()
    config = _metadata_processor_test_config(tmp_path, data_dir)
    processor = ZoteroMetadataProcessor(config)
    processor._library_configs = lambda **_kwargs: [config]  # type: ignore[method-assign]

    result = processor.metadata_backlog_scan(limit=10)

    classifications = [entry["classification"] for entry in result["results"]]
    assert result["scanned"] == 2
    assert result["queued"] == 1
    assert classifications.count("queued") == 1
    assert classifications.count("already_known") == 1
    assert processor.state.metadata_queue_summary(job_type="enrich")["queued"] == 1


def test_local_zotero_iter_regular_items_and_inventory(tmp_path: Path) -> None:
    data_dir = _write_full_text_fixture(tmp_path, with_pdf=True)
    config = _metadata_processor_test_config(tmp_path, data_dir)
    store = LocalZoteroStore(config)

    items = list(store.iter_regular_items(max_items=10))
    inventory = store.item_full_text_inventory(items[0])

    assert [item.key for item in items] == ["PARENT1"]
    assert inventory["has_pdf"] is True
    assert inventory["has_html"] is False

    connection = sqlite3.connect(data_dir / "zotero.sqlite")
    try:
        connection.executescript(
            """
            insert into items values (21, 2, '2026-01-03', 'HTMLMHT1', 2);
            insert into itemAttachments values (21, 10, 0, 'multipart/related', 'storage:article [SOURCE HTML].mhtml');
            """
        )
        connection.commit()
    finally:
        connection.close()
    (data_dir / "storage" / "HTMLMHT1").mkdir(parents=True)
    (data_dir / "storage" / "HTMLMHT1" / "article [SOURCE HTML].mhtml").write_text(
        "<html><body>Article</body></html>",
        encoding="utf-8",
    )

    inventory = store.item_full_text_inventory(items[0])
    assert inventory["has_html"] is True
    assert inventory["has_source_html"] is True
    assert inventory["source_html_count"] == 1


def test_full_text_backlog_scan_queues_parent_items_without_html(
    tmp_path: Path,
) -> None:
    data_dir = _write_full_text_fixture(tmp_path, with_pdf=False)
    config = _metadata_processor_test_config(tmp_path, data_dir)
    processor = ZoteroMetadataProcessor(config)
    processor._library_configs = lambda **_kwargs: [config]  # type: ignore[method-assign]

    result = processor.full_text_backlog_scan(limit=1)

    assert result["queued"] == 1
    assert result["results"][0]["parent_item_key"] == "PARENT1"
    assert result["results"][0]["classification"] == "queued"


def test_full_text_backlog_scan_queues_parent_items_with_html_but_without_pdf(
    tmp_path: Path,
) -> None:
    data_dir = _write_full_text_fixture(tmp_path, with_pdf=False)
    _add_full_text_html_attachment(data_dir)
    config = _metadata_processor_test_config(tmp_path, data_dir)
    processor = ZoteroMetadataProcessor(config)
    processor._library_configs = lambda **_kwargs: [config]  # type: ignore[method-assign]

    result = processor.full_text_backlog_scan(limit=1)

    assert result["queued"] == 1
    assert result["results"][0]["parent_item_key"] == "PARENT1"
    assert result["results"][0]["classification"] == "queued"
    assert result["results"][0]["inventory"]["has_html"] is True
    assert result["results"][0]["inventory"]["has_pdf"] is False
    assert result["results"][0]["inventory"]["has_source_html"] is True


def test_full_text_backlog_scan_queues_parent_items_with_pdf_but_without_source_html(
    tmp_path: Path,
) -> None:
    data_dir = _write_full_text_fixture(tmp_path, with_pdf=True)
    _add_full_text_html_attachment(data_dir, filename="publisher_snapshot.html")
    config = _metadata_processor_test_config(tmp_path, data_dir)
    processor = ZoteroMetadataProcessor(config)
    processor._library_configs = lambda **_kwargs: [config]  # type: ignore[method-assign]

    result = processor.full_text_backlog_scan(limit=1)

    assert result["queued"] == 1
    assert result["results"][0]["parent_item_key"] == "PARENT1"
    assert result["results"][0]["classification"] == "queued"
    assert result["results"][0]["inventory"]["has_pdf"] is True
    assert result["results"][0]["inventory"]["has_html"] is True
    assert result["results"][0]["inventory"]["has_source_html"] is False


def test_full_text_backlog_scan_skips_parent_items_with_html_and_pdf(
    tmp_path: Path,
) -> None:
    data_dir = _write_full_text_fixture(tmp_path, with_pdf=True)
    _add_full_text_html_attachment(data_dir)
    config = _metadata_processor_test_config(tmp_path, data_dir)
    processor = ZoteroMetadataProcessor(config)
    processor._library_configs = lambda **_kwargs: [config]  # type: ignore[method-assign]

    result = processor.full_text_backlog_scan(limit=1)

    assert result["queued"] == 0
    assert result["results"][0]["parent_item_key"] == "PARENT1"
    assert result["results"][0]["classification"] == "html_exists"
    assert result["results"][0]["inventory"]["has_html"] is True
    assert result["results"][0]["inventory"]["has_pdf"] is True
    assert result["results"][0]["inventory"]["has_source_html"] is True


def test_full_text_backlog_scan_respects_remote_parent_filter_alias(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    data_dir = _write_full_text_fixture(tmp_path, with_pdf=False)
    _add_second_parent(
        data_dir, key="PARENT2", title="Filtered Parent", doi="10.2000/filtered"
    )
    monkeypatch.setenv(
        "ZFR_LIBRARY_BINDINGS",
        json.dumps([{"libraryId": "REMOTE_LIB", "dataDir": str(data_dir)}]),
    )
    config = _metadata_processor_test_config(tmp_path, data_dir)
    processor = ZoteroMetadataProcessor(config)
    processor._library_configs = lambda **_kwargs: [config]  # type: ignore[method-assign]

    result = processor.full_text_backlog_scan(
        limit=10,
        only_parent_keys_by_library={"REMOTE_LIB": ["PARENT2"]},
    )

    assert result["scanned"] == 1
    assert result["queued"] == 1
    assert result["results"][0]["parent_item_key"] == "PARENT2"


def test_full_text_backlog_scan_skips_library_when_remote_filter_has_no_alias(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    data_dir = _write_full_text_fixture(tmp_path, with_pdf=False)
    monkeypatch.setenv(
        "ZFR_LIBRARY_BINDINGS",
        json.dumps([{"libraryId": "REMOTE_LIB", "dataDir": str(data_dir)}]),
    )
    config = _metadata_processor_test_config(tmp_path, data_dir)
    processor = ZoteroMetadataProcessor(config)
    processor._library_configs = lambda **_kwargs: [config]  # type: ignore[method-assign]

    result = processor.full_text_backlog_scan(
        limit=10,
        only_parent_keys_by_library={"OTHER_REMOTE_LIB": ["PARENT1"]},
    )

    assert result["scanned"] == 0
    assert result["queued"] == 0
    assert result["results"] == []


def test_scihub_backlog_scan_skips_parent_items_with_pdf(tmp_path: Path) -> None:
    data_dir = _write_full_text_fixture(tmp_path, with_pdf=True)
    config = _metadata_processor_test_config(tmp_path, data_dir)
    processor = ZoteroMetadataProcessor(config)
    processor._library_configs = lambda **_kwargs: [config]  # type: ignore[method-assign]

    result = processor.scihub_pdf_backlog_scan(limit=10)

    assert result["queued"] == 0
    assert result["results"][0]["classification"] == "pdf_exists"


def test_scihub_backlog_force_replaces_existing_pdf(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    data_dir = _write_full_text_fixture(tmp_path, with_pdf=True)
    config = _metadata_processor_test_config(tmp_path, data_dir)
    processor = ZoteroMetadataProcessor(config)
    processor._library_configs = lambda **_kwargs: [config]  # type: ignore[method-assign]
    force_attach_values: list[bool] = []

    def fake_download_and_attach_scihub_pdf(
        config: Any, options: Any
    ) -> dict[str, Any]:
        force_attach_values.append(bool(options.force_attach))
        return {
            "ok": True,
            "status": "attached",
            "download": {"ok": True, "output_path": str(tmp_path / "scihub.pdf")},
            "attach": {"ok": True},
        }

    monkeypatch.setattr(
        "zotero_ingest_worker.metadata_processor.download_and_attach_scihub_pdf",
        fake_download_and_attach_scihub_pdf,
    )

    result = processor.scihub_pdf_backlog_scan(limit=1, force=True)
    queued = processor.state.list_metadata_jobs(
        job_type="scihub_pdf",
        statuses={"queued"},
        limit=1,
    )
    drained = processor.drain_scihub_pdf_queue(limit=1, require_relay=False)

    assert result["queued"] == 1
    assert queued[0]["force"] == 1
    assert drained["results"][0]["status"] == "succeeded"
    assert force_attach_values == [True]


def test_scihub_backlog_scan_respects_remote_parent_filter_alias(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    data_dir = _write_full_text_fixture(tmp_path, with_pdf=False)
    _add_second_parent(
        data_dir, key="PARENT2", title="Filtered Parent", doi="10.2000/filtered"
    )
    monkeypatch.setenv(
        "ZFR_LIBRARY_BINDINGS",
        json.dumps([{"libraryId": "REMOTE_LIB", "dataDir": str(data_dir)}]),
    )
    config = _metadata_processor_test_config(tmp_path, data_dir)
    processor = ZoteroMetadataProcessor(config)
    processor._library_configs = lambda **_kwargs: [config]  # type: ignore[method-assign]

    result = processor.scihub_pdf_backlog_scan(
        limit=10,
        only_parent_keys_by_library={"REMOTE_LIB": ["PARENT2"]},
    )
    queued_jobs = processor.state.list_metadata_jobs(job_type="scihub_pdf", limit=20)

    assert result["scanned"] == 1
    assert result["queued"] == 1
    assert result["results"][0]["parent_item_key"] == "PARENT2"
    assert len(queued_jobs) == 1
    assert queued_jobs[0]["parent_item_key"] == "PARENT2"


def test_scihub_backlog_scan_queues_identifier_queries_when_html_exists_but_pdf_missing(
    tmp_path: Path,
) -> None:
    data_dir = _write_full_text_fixture(tmp_path, with_pdf=False)
    _add_full_text_html_attachment(data_dir)
    connection = sqlite3.connect(data_dir / "zotero.sqlite")
    try:
        connection.executescript(
            """
            insert into fields values (3, 'extra');
            insert into itemDataValues values
              (3, 'PMID: 31044789; PMCID: PMC1234567; https://pubmed.ncbi.nlm.nih.gov/31044789/');
            insert into itemData values (10, 3, 3);
            """
        )
        connection.commit()
    finally:
        connection.close()
    config = _metadata_processor_test_config(tmp_path, data_dir)
    processor = ZoteroMetadataProcessor(config)
    processor._library_configs = lambda **_kwargs: [config]  # type: ignore[method-assign]

    result = processor.scihub_pdf_backlog_scan(limit=10)
    queued_jobs = processor.state.list_metadata_jobs(job_type="scihub_pdf", limit=20)
    queue_keys = [str(job["queue_key"]) for job in queued_jobs]

    assert result["queued"] == 1
    assert len(queued_jobs) == 1
    assert result["results"][0]["inventory"]["has_html"] is True
    assert result["results"][0]["inventory"]["has_pdf"] is False
    assert any("|query_list=" in key for key in queue_keys)
    assert any("doi:10.1000%2Fexample" in key for key in queue_keys)
    assert any("pmid:31044789" in key for key in queue_keys)
    assert any("pmcid:PMC1234567" in key for key in queue_keys)


def test_scihub_drain_grouped_job_tries_next_identifier(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    data_dir = _write_full_text_fixture(tmp_path, with_pdf=False)
    _add_full_text_html_attachment(data_dir)
    connection = sqlite3.connect(data_dir / "zotero.sqlite")
    try:
        connection.executescript(
            """
            insert into fields values (3, 'extra');
            insert into itemDataValues values (3, 'PMID: 31044789; PMCID: PMC1234567');
            insert into itemData values (10, 3, 3);
            """
        )
        connection.commit()
    finally:
        connection.close()
    config = _metadata_processor_test_config(tmp_path, data_dir)
    processor = ZoteroMetadataProcessor(config)
    processor._library_configs = lambda **_kwargs: [config]  # type: ignore[method-assign]
    processor.scihub_pdf_backlog_scan(limit=10)
    calls: list[str] = []

    def fake_download_and_attach_scihub_pdf(
        config: Any, options: Any
    ) -> dict[str, Any]:
        calls.append(str(options.doi))
        if len(calls) == 1:
            return {"ok": False, "status": "unresolved", "error": "not found"}
        return {
            "ok": True,
            "status": "attached",
            "download": {"ok": True, "output_path": str(tmp_path / "scihub.pdf")},
            "attach": {"ok": True},
        }

    monkeypatch.setattr(
        "zotero_ingest_worker.metadata_processor.download_and_attach_scihub_pdf",
        fake_download_and_attach_scihub_pdf,
    )

    result = processor.drain_scihub_pdf_queue(limit=1, require_relay=False)

    assert result["ok"] is True
    assert calls[0] == "10.1000/example"
    assert calls[1] in {"31044789", "PMC1234567"}
    stored = json.loads(result["results"][0]["result_json"])
    assert [attempt["query"] for attempt in stored["attempts"][:2]] == calls[:2]


def test_scihub_drain_rejects_malformed_success_result(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    data_dir = _write_full_text_fixture(tmp_path, with_pdf=False)
    config = _metadata_processor_test_config(tmp_path, data_dir)
    processor = ZoteroMetadataProcessor(config)
    processor._library_configs = lambda **_kwargs: [config]  # type: ignore[method-assign]
    processor.scihub_pdf_backlog_scan(limit=10)

    monkeypatch.setattr(
        "zotero_ingest_worker.metadata_processor.download_and_attach_scihub_pdf",
        lambda *_args, **_kwargs: {
            "ok": "true",
            "status": "attached",
            "download": {
                "ok": True,
                "output_path": str(tmp_path / "scihub.pdf"),
            },
            "attach": {"ok": True},
        },
    )

    result = processor.drain_scihub_pdf_queue(limit=1, require_relay=False)

    assert result["ok"] is False
    assert result["failed"] == 1
    assert result["results"][0]["status"] == "failed_retryable"
    stored = json.loads(result["results"][0]["result_json"])
    assert stored["attempts"][0]["ok"] is False
    assert stored["attempts"][0]["upstream_ok"] == "true"
    assert stored["attempts"][0]["status"] == "invalid_result"
    assert stored["attempts"][0]["upstream_status"] == "attached"


def test_scihub_drain_marks_all_unresolved_grouped_job_skipped(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    data_dir = _write_full_text_fixture(tmp_path, with_pdf=False)
    _add_full_text_html_attachment(data_dir)
    connection = sqlite3.connect(data_dir / "zotero.sqlite")
    try:
        connection.executescript(
            """
            insert into fields values (3, 'extra');
            insert into itemDataValues values (3, 'PMID: 31044789; PMCID: PMC1234567');
            insert into itemData values (10, 3, 3);
            """
        )
        connection.commit()
    finally:
        connection.close()
    config = _metadata_processor_test_config(tmp_path, data_dir)
    processor = ZoteroMetadataProcessor(config)
    processor._library_configs = lambda **_kwargs: [config]  # type: ignore[method-assign]
    processor.scihub_pdf_backlog_scan(limit=10)

    def fake_download_and_attach_scihub_pdf(
        config: Any, options: Any
    ) -> dict[str, Any]:
        return {"ok": False, "status": "unresolved", "error": "not found"}

    monkeypatch.setattr(
        "zotero_ingest_worker.metadata_processor.download_and_attach_scihub_pdf",
        fake_download_and_attach_scihub_pdf,
    )

    result = processor.drain_scihub_pdf_queue(limit=1, require_relay=False)

    assert result["ok"] is True
    assert result["failed"] == 0
    assert result["results"][0]["status"] == "skipped"
    stored = json.loads(result["results"][0]["result_json"])
    assert stored["reason"] == "scihub_pdf_not_found"
    assert {attempt["status"] for attempt in stored["attempts"]} == {"unresolved"}


def test_scihub_drain_marks_exhausted_fetch_error_skipped(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    data_dir = _write_full_text_fixture(tmp_path, with_pdf=False)
    _add_full_text_html_attachment(data_dir)
    config = _metadata_processor_test_config(tmp_path, data_dir)
    processor = ZoteroMetadataProcessor(config)
    processor._library_configs = lambda **_kwargs: [config]  # type: ignore[method-assign]
    processor.scihub_pdf_backlog_scan(limit=10)
    queued = processor.state.list_metadata_jobs(
        job_type="scihub_pdf", statuses={"queued"}, limit=1
    )[0]
    with processor.state._connect() as connection:
        connection.execute(
            "update metadata_jobs set max_attempts = 1 where job_id = ?",
            (queued["job_id"],),
        )

    def fake_download_and_attach_scihub_pdf(
        config: Any, options: Any
    ) -> dict[str, Any]:
        return {"ok": False, "status": "fetch_error", "error": "timed out"}

    monkeypatch.setattr(
        "zotero_ingest_worker.metadata_processor.download_and_attach_scihub_pdf",
        fake_download_and_attach_scihub_pdf,
    )

    result = processor.drain_scihub_pdf_queue(limit=1, require_relay=False)

    assert result["ok"] is True
    assert result["failed"] == 0
    assert result["results"][0]["status"] == "skipped"
    stored = json.loads(result["results"][0]["result_json"])
    assert stored["reason"] == "scihub_pdf_retry_exhausted"
    assert stored["status"] == "fetch_error"


def test_full_text_backlog_scan_dedupes_parent_when_sqlite_mtime_changes(
    tmp_path: Path,
) -> None:
    data_dir = _write_full_text_fixture(tmp_path, with_pdf=False)
    config = _metadata_processor_test_config(tmp_path, data_dir)
    processor = ZoteroMetadataProcessor(config)
    processor._library_configs = lambda **_kwargs: [config]  # type: ignore[method-assign]

    first = processor.full_text_backlog_scan(limit=1)
    (data_dir / "zotero.sqlite").touch()
    second = processor.full_text_backlog_scan(limit=1)

    assert first["queued"] == 1
    assert second["queued"] == 0
    assert second["results"][0]["classification"] == "already_known"
    assert processor.state.metadata_queue_summary(job_type="full_text")["queued"] == 1


def test_full_text_drain_parent_job_uses_inventory_to_avoid_pdf_duplicate(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    data_dir = _write_full_text_fixture(tmp_path, with_pdf=True)
    config = _metadata_processor_test_config(tmp_path, data_dir)
    processor = ZoteroMetadataProcessor(config)
    processor._library_configs = lambda **_kwargs: [config]  # type: ignore[method-assign]
    processor.full_text_backlog_scan(limit=1)
    captured: dict[str, Any] = {}

    class FakeFullTextResult:
        status = "html_found"

        def to_dict(self) -> dict[str, Any]:
            return {
                "status": self.status,
                "html_downloads": [
                    {
                        "ok": True,
                        "output_path": str(tmp_path / "found.html"),
                        "article": _valid_full_article_assessment(),
                    }
                ],
                "pdf_downloads": [],
            }

    def fake_discover_and_download_full_text(**kwargs: Any) -> FakeFullTextResult:
        captured.update(kwargs)
        return FakeFullTextResult()

    monkeypatch.setattr(
        "zotero_ingest_worker.metadata_processor.discover_and_download_full_text",
        fake_discover_and_download_full_text,
    )

    result = processor.drain_full_text_queue(limit=1, require_relay=False)

    assert result["processed"] == 1
    assert captured["metadata"].key == "PARENT1"
    assert captured["attachment"].key == "PARENT1"
    assert captured["max_pdf_downloads"] == 0
    assert "items" in str(captured["output_dir"])
    stored = json.loads(result["results"][0]["result_json"])
    assert stored["source_context"] == "parent_item"
    assert stored["existing_full_text_inventory"]["has_pdf"] is True


def test_rejected_full_text_html_does_not_enqueue_existing_pdf_conversion(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    data_dir = _write_full_text_fixture(tmp_path, with_pdf=True)
    config = _metadata_processor_test_config(tmp_path, data_dir)
    processor = ZoteroMetadataProcessor(config)
    processor._library_configs = lambda **_kwargs: [config]  # type: ignore[method-assign]
    processor.full_text_backlog_scan(limit=1)

    class FakeFullTextResult:
        status = "html_found"
        discovery = None

        def to_dict(self) -> dict[str, Any]:
            return {
                "status": self.status,
                "html_downloads": [
                    {
                        "ok": True,
                        "status": "downloaded",
                        "output_path": str(tmp_path / "landing.html"),
                        "article_verdict": {"ok": False, "reason": "weak_landing"},
                    }
                ],
                "pdf_downloads": [],
            }

    monkeypatch.setattr(
        "zotero_ingest_worker.metadata_processor.discover_and_download_full_text",
        lambda **_kwargs: FakeFullTextResult(),
    )

    result = processor.drain_full_text_queue(limit=1, require_relay=False)
    stored = json.loads(result["results"][0]["result_json"])

    assert result["processed"] == 1
    assert stored["worker_status"] == "html_rejected"
    assert "existing_pdf_enqueue" not in stored
    assert processor.state.html_queue_summary()["queued"] == 0


def test_existing_pdf_worker_status_reports_html_precheck_without_job() -> None:
    payload = {
        "html_downloads": [
            {"ok": True, "article_verdict": {"ok": False, "reason": "weak_landing"}}
        ],
        "pdf_downloads": [],
        "existing_pdf_enqueue": {
            "ok": True,
            "html_enqueue": {"classification": "source_language_skipped"},
        },
    }

    assert (
        full_text_worker_status(payload) == "existing_pdf_html_source_language_skipped"
    )


def test_full_text_worker_status_reports_html_and_pdf_together() -> None:
    payload = {
        "html_downloads": [
            {
                "ok": True,
                "output_path": "/tmp/article.html",
                "article": _valid_full_article_assessment(),
            }
        ],
        "pdf_downloads": [
            {
                "ok": True,
                "output_path": "/tmp/article.pdf",
                "identity": {"needs_ocr": False},
            }
        ],
    }

    assert full_text_worker_status(payload) == "html_and_pdf_found"


def test_parent_attachment_relay_payload_includes_parent_item_key(
    monkeypatch: Any, tmp_path: Path
) -> None:
    source = tmp_path / "article.html"
    source.write_text("<html></html>", encoding="utf-8")
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(zotero_relay_url="http://relay")
    captured: dict[str, object] = {}

    def fake_request_json(
        self: ZoteroRelayClient, **kwargs: object
    ) -> dict[str, object]:
        captured.update(kwargs)
        return {"ok": True, "newAttachmentKey": "HTML1234"}

    monkeypatch.setattr(ZoteroRelayClient, "request_json", fake_request_json)
    metadata = SimpleNamespace(library_id="LIB1", key="ITEM1234")
    attachment = SimpleNamespace(state_key="LIB1_PDF1234")

    result = processor._create_parent_attachment_via_relay(
        metadata=metadata,
        attachment=attachment,
        source_path=source,
        filename="article.html",
        title="Article",
        content_type="text/html",
        probe_attachment_key="PDF1234",
        dedupe_prefix="full-text-html",
        source_sha256="A" * 64,
    )

    assert result == {"ok": True, "newAttachmentKey": "HTML1234"}
    assert captured["path"] == "/attachments/parents/ITEM1234/attachments/file"
    payload = captured["payload"]
    assert payload["libraryId"] == "LIB1"  # type: ignore[index]
    assert payload["contentType"] == "text/html"  # type: ignore[index]
    assert payload["probeAttachmentKey"] == "PDF1234"  # type: ignore[index]
    assert payload["deduplicationKey"] == (  # type: ignore[index]
        "full-text-html:LIB1:ITEM1234:sha256:" + "a" * 64
    )
    assert payload["sourceSha256"] == "a" * 64  # type: ignore[index]


@pytest.mark.parametrize("source_sha256", ["", "abc", "g" * 64, "a" * 63])
def test_parent_attachment_relay_rejects_invalid_explicit_source_sha256(
    monkeypatch: Any,
    tmp_path: Path,
    source_sha256: str,
) -> None:
    source = tmp_path / "article.html"
    source.write_text("<html></html>", encoding="utf-8")
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(zotero_relay_url="http://relay")
    request_calls: list[dict[str, object]] = []

    def fake_request_json(
        self: ZoteroRelayClient,
        **kwargs: object,
    ) -> dict[str, object]:
        request_calls.append(kwargs)
        return {"ok": True, "newAttachmentKey": "HTML1234"}

    monkeypatch.setattr(ZoteroRelayClient, "request_json", fake_request_json)

    with pytest.raises(ValueError, match="source_sha256"):
        processor._create_parent_attachment_via_relay(
            metadata=SimpleNamespace(library_id="LIB1", key="ITEM1234"),
            attachment=SimpleNamespace(state_key="LIB1_PDF1234"),
            source_path=source,
            filename="article.html",
            title="Article",
            content_type="text/html",
            probe_attachment_key="PDF1234",
            dedupe_prefix="full-text-html",
            source_sha256=source_sha256,
        )

    assert request_calls == []


def test_parent_attachment_relay_preserves_legacy_stat_identity_without_sha256(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.pdf"
    source.write_bytes(b"PDF")
    source_stat = source.stat()
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(zotero_relay_url="http://relay")
    captured: dict[str, object] = {}

    def fake_request_json(
        self: ZoteroRelayClient,
        **kwargs: object,
    ) -> dict[str, object]:
        captured.update(kwargs)
        return {"ok": True, "newAttachmentKey": "PDF1234"}

    monkeypatch.setattr(ZoteroRelayClient, "request_json", fake_request_json)

    result = processor._create_parent_attachment_via_relay(
        metadata=SimpleNamespace(library_id="LIB1", key="ITEM1234"),
        attachment=SimpleNamespace(state_key="LIB1_PDF1234"),
        source_path=source,
        filename="article.pdf",
        title="Article",
        content_type="application/pdf",
        probe_attachment_key="PDF1234",
        dedupe_prefix="full-text-pdf",
        source_sha256=None,
    )

    assert result == {"ok": True, "newAttachmentKey": "PDF1234"}
    payload = captured["payload"]
    assert payload["deduplicationKey"] == (  # type: ignore[index]
        f"full-text-pdf:LIB1:ITEM1234:{source_stat.st_size}:{source_stat.st_mtime_ns}"
    )

    assert "sourceSha256" not in payload  # type: ignore[operator]


def test_html_attachment_source_embeds_local_assets(tmp_path: Path) -> None:
    source = tmp_path / "01.source.html"
    assets_dir = tmp_path / "01.source_assets"
    assets_dir.mkdir()
    (assets_dir / "fig.png").write_bytes(b"PNG")
    (assets_dir / "style.css").write_text(
        "body { background: url(fig.png); }", encoding="utf-8"
    )
    source.write_text(
        (
            '<html><head><link rel="stylesheet" href="01.source_assets/style.css"></head>'
            '<body><img src="01.source_assets/fig.png"></body></html>'
        ),
        encoding="utf-8",
    )

    embedded_path, report = _html_attachment_source_with_embedded_assets(source)

    assert embedded_path != source
    assert embedded_path.exists()
    assert report["enabled"] is True
    assert report["embedded_assets"] == 1
    assert report["embedded_stylesheets"] == 1
    saved = embedded_path.read_text(encoding="utf-8")
    assert "_assets/" not in saved
    assert "<style>" in saved
    assert "data:image/png;base64," in saved


def test_html_attachment_source_does_not_rewrite_asset_path_in_text_or_comment(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "figure.png").write_bytes(b"PNG")
    source.write_text(
        (
            "<html><body><p>Keep article_assets/figure.png as prose.</p>"
            '<!-- src="article_assets/figure.png" -->'
            '<img src="article_assets/figure.png">'
            '<div style="background:url(article_assets/figure.png)"></div>'
            "</body></html>"
        ),
        encoding="utf-8",
    )

    embedded, report = _html_attachment_source_with_embedded_assets(source)

    assert report["enabled"] is True
    saved = embedded.read_text(encoding="utf-8")
    assert "Keep article_assets/figure.png as prose." in saved
    assert '<!-- src="article_assets/figure.png" -->' in saved
    assert saved.count("data:image/png;base64,") == 2


def test_html_attachment_source_rewrites_supported_resource_contexts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "figure.png").write_bytes(b"PNG")
    source.write_text(
        (
            '<html><head><style>.hero{background:url("article_assets/figure.png")}</style></head>'
            '<body><img src="./article_assets/figure.png?rev=1">'
            '<source srcset="article_assets/figure.png, article_assets/figure.png?rev=2 2x">'
            '<video poster="article_assets/figure.png"></video>'
            '<object data="article_assets/figure.png"></object>'
            '<svg><image xlink:href="article_assets/figure.png#part"></image></svg>'
            '<a href="article_assets/figure.png">download</a>'
            '<div style="background:url(article_assets/figure.png)"></div>'
            "</body></html>"
        ),
        encoding="utf-8",
    )

    embedded, report = _html_attachment_source_with_embedded_assets(source)

    assert report["enabled"] is True
    saved = embedded.read_text(encoding="utf-8")
    assert saved.count("data:image/png;base64,") == 9
    assert "article_assets/figure.png" not in saved
    assert "#part" in saved


def test_html_attachment_source_preserves_inert_html_and_css_contexts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "figure.png").write_bytes(b"PNG")
    source.write_text(
        (
            "<html><head><style>/* url(article_assets/figure.png) */"
            '.label::before{content:"url(article_assets/figure.png)"}</style></head>'
            "<body><p>article_assets/figure.png</p>"
            '<!-- <img src="article_assets/figure.png"> -->'
            '<script>const sample = "<img src=article_assets/figure.png>";'
            'const css = "url(article_assets/figure.png)";</script>'
            '<textarea><img src="article_assets/figure.png"></textarea>'
            '<div title="article_assets/figure.png">label</div>'
            '<img src="article_assets/figure.png">'
            "</body></html>"
        ),
        encoding="utf-8",
    )

    embedded, report = _html_attachment_source_with_embedded_assets(source)

    assert report["enabled"] is True
    saved = embedded.read_text(encoding="utf-8")
    assert "<p>article_assets/figure.png</p>" in saved
    assert '<!-- <img src="article_assets/figure.png"> -->' in saved
    assert 'const sample = "<img src=article_assets/figure.png>";' in saved
    assert 'const css = "url(article_assets/figure.png)";' in saved
    assert '<textarea><img src="article_assets/figure.png"></textarea>' in saved
    assert 'title="article_assets/figure.png"' in saved
    assert "/* url(article_assets/figure.png) */" in saved
    assert 'content:"url(article_assets/figure.png)"' in saved
    assert saved.count("data:image/png;base64,") == 1


def test_html_attachment_source_ignores_inert_local_refs_without_assets(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    source.write_text(
        (
            "<html><head><style>/* url(missing.png) */"
            '.label::before{content:"url(missing.png)"}</style></head>'
            "<body><p>missing.png</p>"
            '<!-- <img src="missing.png"> -->'
            '<script>const sample = "<img src=missing.png>";</script>'
            '<textarea><img src="missing.png"></textarea>'
            "</body></html>"
        ),
        encoding="utf-8",
    )

    attachment_source, report = _html_attachment_source_with_embedded_assets(source)

    assert attachment_source == source
    assert report == {"enabled": False, "reason": "assets_dir_missing"}


def test_html_attachment_source_inlines_style_import_assets(tmp_path: Path) -> None:
    source = tmp_path / "01.source.html"
    assets_dir = tmp_path / "01.source_assets"
    assets_dir.mkdir()
    (assets_dir / "fig.png").write_bytes(b"PNG")
    (assets_dir / "base.css").write_text(
        "body { background: url(fig.png); }", encoding="utf-8"
    )
    (assets_dir / "style.css").write_text(
        '@import "base.css" layer(base); .article { color: red; }',
        encoding="utf-8",
    )
    source.write_text(
        (
            '<html><head><style>@import "01.source_assets/style.css" layer(article);</style></head>'
            '<body><img src="01.source_assets/fig.png"></body></html>'
        ),
        encoding="utf-8",
    )

    embedded_path, report = _html_attachment_source_with_embedded_assets(source)

    assert embedded_path != source
    assert embedded_path.name.startswith("z2m_embedded.")
    assert embedded_path.name.endswith(".html")
    assert report["enabled"] is True
    assert report["embedded_stylesheets"] == 2
    saved = embedded_path.read_text(encoding="utf-8")
    assert "@import" not in saved
    assert "@layer article" in saved
    assert "@layer base" in saved
    assert ".article { color: red; }" in saved
    assert "data:image/png;base64," in saved


def test_html_attachment_source_external_css_preserves_inert_references(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "figure.png").write_bytes(b"PNG")
    (assets_dir / "style.css").write_text(
        (
            '/* url(missing.png); @import "missing.css"; */'
            '.label::before{content:"url(missing.png)"}'
            ".hero{background:url(figure.png)}"
        ),
        encoding="utf-8",
    )
    source.write_text(
        '<html><head><link rel="stylesheet" href="article_assets/style.css"></head></html>',
        encoding="utf-8",
    )

    embedded, report = _html_attachment_source_with_embedded_assets(source)

    assert report["enabled"] is True
    assert report["missing_local_refs"] == []
    saved = embedded.read_text(encoding="utf-8")
    assert '/* url(missing.png); @import "missing.css"; */' in saved
    assert 'content:"url(missing.png)"' in saved
    assert saved.count("data:image/png;base64,") == 1


def test_html_attachment_source_inlines_quoted_css_import_with_space(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "figure.png").write_bytes(b"PNG")
    (assets_dir / "base sheet.css").write_text(
        ".hero{background:url(figure.png)}",
        encoding="utf-8",
    )
    (assets_dir / "style.css").write_text(
        '@import "base sheet.css" layer(base); .article{color:red}',
        encoding="utf-8",
    )
    source.write_text(
        '<html><head><link rel="stylesheet" href="article_assets/style.css"></head></html>',
        encoding="utf-8",
    )

    embedded, report = _html_attachment_source_with_embedded_assets(source)

    assert report["enabled"] is True
    assert report["embedded_stylesheets"] == 2
    assert report["missing_local_refs"] == []
    saved = embedded.read_text(encoding="utf-8")
    assert "@import" not in saved
    assert "@layer base" in saved
    assert ".article{color:red}" in saved
    assert saved.count("data:image/png;base64,") == 1


def test_html_attachment_source_publishes_content_addressed_snapshot(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "figure.png").write_bytes(b"PNG")
    source.write_text(
        '<html><body><img src="article_assets/figure.png"></body></html>',
        encoding="utf-8",
    )

    first_path, first_report = _html_attachment_source_with_embedded_assets(source)
    second_path, second_report = _html_attachment_source_with_embedded_assets(source)

    output = first_report["output"]
    assert first_path == second_path
    assert first_path.name == f"z2m_embedded.{output['sha256']}.html"
    assert output["bytes"] == first_path.stat().st_size
    assert len(output["sha256"]) == 64
    assert first_report["cache_reused"] is False
    assert second_report["cache_reused"] is True
    assert not list(tmp_path.glob("*.z2m-embedded-tmp-*"))


def test_html_attachment_source_bounds_content_addressed_filename(
    tmp_path: Path,
) -> None:
    stem = "long-source-name-" + "x" * 160
    source = tmp_path / f"{stem}.html"
    assets_dir = tmp_path / f"{stem}_assets"
    assets_dir.mkdir()
    (assets_dir / "figure.png").write_bytes(b"PNG")
    source.write_text(
        f'<html><body><img src="{assets_dir.name}/figure.png"></body></html>',
        encoding="utf-8",
    )

    embedded, report = _html_attachment_source_with_embedded_assets(source)

    assert report["enabled"] is True
    assert len(embedded.name) <= 255
    assert embedded.name.endswith(f".{report['output']['sha256']}.html")
    assert embedded.read_text(encoding="utf-8").count("data:image/png;base64,") == 1


def test_html_attachment_source_cleans_output_when_encoded_size_exceeds_limit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "figure.png").write_bytes(b"PNG")
    source.write_text(
        '<html><body><img src="article_assets/figure.png"></body></html>',
        encoding="utf-8",
    )

    embedded, report = _html_attachment_source_with_embedded_assets(
        source,
        max_output_bytes=32,
    )

    assert embedded == source
    assert report["failed"] is True
    assert report["reason"] == "output_too_large"
    assert report["max_output_bytes"] == 32
    assert not list(tmp_path.glob("*z2m_embedded*"))
    assert not list(tmp_path.glob("*.z2m-embedded-tmp-*"))


def test_html_attachment_source_bounds_rewrite_before_output_publication(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "figure.png").write_bytes(b"PNG" * 32)
    repeated_images = "".join(
        '<img src="article_assets/figure.png">' for _ in range(20)
    )
    source.write_text(
        f"<html><body>{repeated_images}</body></html>",
        encoding="utf-8",
    )
    publish_calls: list[str] = []

    def reject_late_publication(*_args: object, **_kwargs: object) -> object:
        publish_calls.append("called")
        raise AssertionError("oversized rewrite reached publication")

    monkeypatch.setattr(
        full_text_attachment_module,
        "_publish_embedded_html_snapshot",
        reject_late_publication,
    )

    embedded, report = _html_attachment_source_with_embedded_assets(
        source,
        max_output_bytes=512,
    )

    assert embedded == source
    assert report["failed"] is True
    assert report["reason"] == "output_too_large"
    assert report["max_output_bytes"] == 512
    assert publish_calls == []


def test_html_attachment_source_repairs_tampered_content_addressed_snapshot(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "figure.png").write_bytes(b"PNG")
    source.write_text(
        '<html><body><img src="article_assets/figure.png"></body></html>',
        encoding="utf-8",
    )
    embedded, first_report = _html_attachment_source_with_embedded_assets(source)
    expected = embedded.read_bytes()
    embedded.write_bytes(b"tampered-cache")

    repaired, repair_report = _html_attachment_source_with_embedded_assets(source)

    assert repair_report["enabled"] is True
    assert repair_report["cache_reused"] is False
    assert repair_report["output"] == first_report["output"]
    assert repaired == embedded
    assert repaired.read_bytes() == expected
    assert not list(tmp_path.glob("*.corrupt-*"))


def test_html_attachment_source_concurrent_writers_share_exact_snapshot(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "figure.png").write_bytes(b"PNG")
    source.write_text(
        '<html><body><img src="article_assets/figure.png"></body></html>',
        encoding="utf-8",
    )
    worker_count = 6
    barrier = threading.Barrier(worker_count)
    original_replace = (
        full_text_attachment_module._replace_stylesheet_links_with_style_tags
    )

    def synchronized_replace(
        html_text: str,
        css_by_rel: dict[str, str],
        **kwargs: Any,
    ) -> str:
        barrier.wait(timeout=5)
        return original_replace(html_text, css_by_rel, **kwargs)

    monkeypatch.setattr(
        full_text_attachment_module,
        "_replace_stylesheet_links_with_style_tags",
        synchronized_replace,
    )
    outcomes: list[tuple[Path, dict[str, Any]]] = []
    errors: list[BaseException] = []
    result_lock = threading.Lock()

    def run() -> None:
        try:
            result = _html_attachment_source_with_embedded_assets(source)
        except BaseException as exc:
            with result_lock:
                errors.append(exc)
        else:
            with result_lock:
                outcomes.append(result)

    workers = [threading.Thread(target=run) for _ in range(worker_count)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    assert len(outcomes) == worker_count
    assert len({path for path, _report in outcomes}) == 1, outcomes
    assert all(report["enabled"] is True for _path, report in outcomes), outcomes
    assert sum(report["cache_reused"] is False for _path, report in outcomes) == 1
    assert not list(tmp_path.glob("*.z2m-embedded-tmp-*"))


def test_html_attachment_source_rejects_source_mutation_before_publication(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "figure.png").write_bytes(b"PNG")
    source.write_text(
        '<html><body><img src="article_assets/figure.png"></body></html>',
        encoding="utf-8",
    )
    entered = threading.Event()
    release = threading.Event()
    original_replace = (
        full_text_attachment_module._replace_stylesheet_links_with_style_tags
    )

    def blocking_replace(
        html_text: str,
        css_by_rel: dict[str, str],
        **kwargs: Any,
    ) -> str:
        entered.set()
        assert release.wait(timeout=5)
        return original_replace(html_text, css_by_rel, **kwargs)

    monkeypatch.setattr(
        full_text_attachment_module,
        "_replace_stylesheet_links_with_style_tags",
        blocking_replace,
    )
    outcome: list[tuple[Path, dict[str, Any]]] = []
    worker = threading.Thread(
        target=lambda: outcome.append(
            _html_attachment_source_with_embedded_assets(source)
        )
    )
    worker.start()
    assert entered.wait(timeout=5)
    source.write_text("<html><body>changed</body></html>", encoding="utf-8")
    release.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    attachment_source, report = outcome[0]
    assert attachment_source == source
    assert report["failed"] is True
    assert report["reason"] == "source_changed"
    assert not list(tmp_path.glob("*z2m_embedded*"))


def test_html_attachment_source_rejects_asset_mutation_before_publication(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    figure = assets_dir / "figure.png"
    figure.write_bytes(b"PNG")
    source.write_text(
        '<html><body><img src="article_assets/figure.png"></body></html>',
        encoding="utf-8",
    )
    entered = threading.Event()
    release = threading.Event()
    original_replace = (
        full_text_attachment_module._replace_stylesheet_links_with_style_tags
    )

    def blocking_replace(
        html_text: str,
        css_by_rel: dict[str, str],
        **kwargs: Any,
    ) -> str:
        entered.set()
        assert release.wait(timeout=5)
        return original_replace(html_text, css_by_rel, **kwargs)

    monkeypatch.setattr(
        full_text_attachment_module,
        "_replace_stylesheet_links_with_style_tags",
        blocking_replace,
    )
    outcome: list[tuple[Path, dict[str, Any]]] = []
    worker = threading.Thread(
        target=lambda: outcome.append(
            _html_attachment_source_with_embedded_assets(source)
        )
    )
    worker.start()
    assert entered.wait(timeout=5)
    figure.write_bytes(b"CHANGED")
    release.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    attachment_source, report = outcome[0]
    assert attachment_source == source
    assert report["failed"] is True
    assert report["reason"] == "asset_changed"
    assert not list(tmp_path.glob("*z2m_embedded*"))


def test_html_attachment_source_retains_complete_snapshot_on_cancellation(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "figure.png").write_bytes(b"PNG")
    source.write_text(
        '<html><body><img src="article_assets/figure.png"></body></html>',
        encoding="utf-8",
    )
    original_link = full_text_attachment_module.os.link

    def link_then_cancel(*args: object, **kwargs: object) -> None:
        original_link(*args, **kwargs)  # type: ignore[arg-type]
        raise KeyboardInterrupt

    monkeypatch.setattr(
        full_text_attachment_module.os,
        "link",
        link_then_cancel,
    )

    with pytest.raises(KeyboardInterrupt):
        _html_attachment_source_with_embedded_assets(source)

    snapshots = list(tmp_path.glob("z2m_embedded.*.html"))
    assert len(snapshots) == 1
    assert "data:image/png;base64," in snapshots[0].read_text(encoding="utf-8")
    assert not list(tmp_path.glob("*.z2m-embedded-tmp-*"))


def test_html_attachment_cancellation_does_not_invalidate_concurrent_cache_reader(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "figure.png").write_bytes(b"PNG")
    source.write_text(
        '<html><body><img src="article_assets/figure.png"></body></html>',
        encoding="utf-8",
    )
    original_link = full_text_attachment_module.os.link
    linked = threading.Event()
    release_cancel = threading.Event()
    cancelling_thread: list[threading.Thread] = []
    cancellation_errors: list[BaseException] = []

    def link_then_wait_and_cancel(*args: object, **kwargs: object) -> None:
        if threading.current_thread() is cancelling_thread[0]:
            original_link(*args, **kwargs)  # type: ignore[arg-type]
            linked.set()
            assert release_cancel.wait(timeout=5)
            raise KeyboardInterrupt
        original_link(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        full_text_attachment_module.os,
        "link",
        link_then_wait_and_cancel,
    )

    def cancel_after_publish() -> None:
        try:
            _html_attachment_source_with_embedded_assets(source)
        except BaseException as exc:
            cancellation_errors.append(exc)

    worker = threading.Thread(target=cancel_after_publish)
    cancelling_thread.append(worker)
    worker.start()
    assert linked.wait(timeout=5)

    reused_path, reused_report = _html_attachment_source_with_embedded_assets(source)
    assert reused_report["enabled"] is True
    assert reused_report["cache_reused"] is True
    assert reused_path.exists()

    release_cancel.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(cancellation_errors) == 1
    assert isinstance(cancellation_errors[0], KeyboardInterrupt)
    assert reused_path.exists()
    assert "data:image/png;base64," in reused_path.read_text(encoding="utf-8")
    assert not list(tmp_path.glob("*.z2m-embedded-tmp-*"))


def test_full_text_attach_html_uses_embedded_asset_file(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "01.source.html"
    assets_dir = tmp_path / "01.source_assets"
    assets_dir.mkdir()
    (assets_dir / "fig.png").write_bytes(b"PNG")
    source.write_text(
        '<html><body><img src="01.source_assets/fig.png"></body></html>',
        encoding="utf-8",
    )
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(zotero_relay_url="http://relay")
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Important Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    captured: dict[str, object] = {}

    def fake_create_parent_attachment_via_relay(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "ok": True,
            "newAttachmentKey": "HTML9999",
            "newAttachmentVersion": 1,
        }

    monkeypatch.setattr(
        processor,
        "_create_parent_attachment_via_relay",
        fake_create_parent_attachment_via_relay,
    )

    result = processor._attach_full_text_result(
        attachment=attachment,
        metadata=metadata,
        inventory={"attachments": []},
        payload={
            "html_downloads": [
                {
                    "ok": True,
                    "status": "downloaded",
                    "output_path": str(source),
                    "article": _valid_full_article_assessment(),
                }
            ],
            "pdf_downloads": [],
        },
    )

    assert result is not None
    assert result["kind"] == "html"
    relay_source = Path(captured["source_path"])  # type: ignore[arg-type]
    assert relay_source.name.startswith(".z2m-parent-attachment-snapshot-")
    assert relay_source.suffix == ".html"
    assert "article_packages" in relay_source.parts
    assert result["raw_source_path"] == str(source)
    assert result["article_standard"]["ok"] is True
    assert result["article_standard"]["polish"]["inlined_images"] == 1
    assert captured["content_type"] == "text/html"
    assert result["embedded_assets"] == {
        "enabled": False,
        "reason": "assets_dir_missing",
    }
    local_copy = Path(result["local_copy"]["path"])
    assert local_copy.suffix == ".html"
    saved = local_copy.read_text(encoding="utf-8")
    assert "_assets/" not in saved
    assert "data:image/png;base64," in saved


def test_full_text_attach_stops_when_relay_observes_mutated_html_snapshot(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    source.write_text(
        "<html><head><title>Article</title></head><body><article>Article</article></body></html>",
        encoding="utf-8",
    )
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(zotero_relay_url="http://relay")
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Important Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")

    def mutate_source_during_relay(**kwargs: object) -> dict[str, object]:
        Path(str(kwargs["source_path"])).write_text("tampered", encoding="utf-8")
        return {
            "ok": True,
            "newAttachmentKey": "HTML9999",
            "newAttachmentVersion": 1,
        }

    monkeypatch.setattr(
        processor,
        "_create_parent_attachment_via_relay",
        mutate_source_during_relay,
    )

    result = processor._attach_full_text_result(
        attachment=attachment,
        metadata=metadata,
        inventory={"attachments": []},
        payload={
            "html_downloads": [
                {
                    "ok": True,
                    "status": "downloaded",
                    "output_path": str(source),
                    "article": _valid_full_article_assessment(),
                }
            ],
            "pdf_downloads": [],
        },
    )

    assert result is not None
    assert result["ok"] is False
    assert result["status"] == "attachment_snapshot_changed_after_relay"
    assert not (tmp_path / "storage" / "HTML9999").exists()


def test_html_attachment_source_without_assets_uses_original_file(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    source.write_text("<html><body>Article</body></html>", encoding="utf-8")

    attachment_source, report = _html_attachment_source_with_embedded_assets(source)

    assert attachment_source == source
    assert report == {"enabled": False, "reason": "assets_dir_missing"}


def test_html_attachment_source_rejects_unresolved_ref_without_assets_dir(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    source.write_text(
        '<html><body><img src="missing.png"></body></html>',
        encoding="utf-8",
    )

    attachment_source, report = _html_attachment_source_with_embedded_assets(source)

    assert attachment_source == source
    assert report["enabled"] is False
    assert report["failed"] is True
    assert report["reason"] == "unresolved_local_assets"
    assert report["unresolved_local_refs"] == ["missing.png"]


def test_html_attachment_source_with_empty_assets_uses_original_file(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    source.write_text("<html><body>Article</body></html>", encoding="utf-8")
    (tmp_path / "article_assets").mkdir()

    attachment_source, report = _html_attachment_source_with_embedded_assets(source)

    assert attachment_source == source
    assert report["enabled"] is False
    assert report["reason"] == "assets_empty"


def test_html_attachment_source_rejects_missing_css_asset_even_with_external_url(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "style.css").write_text(
        "body { background: url(missing.png); } .logo { background: url(https://cdn.example/logo.png); }",
        encoding="utf-8",
    )
    source.write_text(
        '<html><head><link rel="stylesheet" href="article_assets/style.css"></head><body></body></html>',
        encoding="utf-8",
    )

    attachment_source, report = _html_attachment_source_with_embedded_assets(source)

    assert attachment_source == source
    assert report["failed"] is True
    assert report["reason"] == "unresolved_local_assets"
    assert report["embedded_stylesheets"] == 1
    assert report["missing_local_refs"] == ["missing.png"]
    assert report["unresolved_local_refs"] == ["missing.png"]


def test_html_attachment_source_rejects_oversized_source_before_embedding(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "figure.png").write_bytes(b"PNG")
    source.write_text(
        '<html><body><img src="article_assets/figure.png"></body></html>',
        encoding="utf-8",
    )

    attachment_source, report = _html_attachment_source_with_embedded_assets(
        source,
        max_source_bytes=16,
    )

    assert attachment_source == source
    assert report["enabled"] is False
    assert report["failed"] is True
    assert report["reason"] == "source_too_large"
    assert report["source_bytes"] > report["max_source_bytes"]


def test_html_attachment_source_skips_asset_over_individual_budget(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "figure.png").write_bytes(b"X" * 17)
    source.write_text(
        '<html><body><img src="article_assets/figure.png"></body></html>',
        encoding="utf-8",
    )

    attachment_source, report = _html_attachment_source_with_embedded_assets(
        source,
        max_asset_bytes=16,
    )

    assert attachment_source == source
    assert report["failed"] is True
    assert report["reason"] == "unresolved_local_assets"
    assert report["unresolved_local_refs"] == ["article_assets/figure.png"]
    assert report["skipped_asset_count"] == 1
    assert report["skipped_assets"][0]["reason"] == "asset_too_large"


def test_html_attachment_source_rejects_candidate_outside_assets_root(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    outside = tmp_path / "host-secret.txt"
    outside.write_text("DO NOT EMBED", encoding="utf-8")
    source.write_text(
        '<html><body><img src="article_assets/host-secret.txt"></body></html>',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        full_text_attachment_module,
        "_local_asset_candidates",
        lambda *_args, **_kwargs: ([outside], False),
    )

    attachment_source, report = _html_attachment_source_with_embedded_assets(source)

    assert attachment_source == source
    assert report["failed"] is True
    assert report["reason"] == "unresolved_local_assets"
    assert report["unresolved_local_refs"] == ["article_assets/host-secret.txt"]
    assert report["skipped_assets"][0]["reason"] == "asset_outside_root"
    assert "DO NOT EMBED" not in source.read_text(encoding="utf-8")


def test_html_attachment_source_enforces_total_asset_budget(tmp_path: Path) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "a.png").write_bytes(b"A" * 8)
    (assets_dir / "b.png").write_bytes(b"B" * 8)
    source.write_text(
        '<html><body><img src="article_assets/a.png"><img src="article_assets/b.png"></body></html>',
        encoding="utf-8",
    )

    attachment_source, report = _html_attachment_source_with_embedded_assets(
        source,
        max_asset_bytes=8,
        max_total_asset_bytes=8,
    )

    assert attachment_source == source
    assert report["failed"] is True
    assert report["reason"] == "unresolved_local_assets"
    assert report["unresolved_local_refs"] == ["article_assets/b.png"]
    assert report["embedded_assets"] == 1
    assert report["embedded_source_bytes"] == 8
    assert report["skipped_assets"][0]["reason"] == "asset_total_bytes_limit"


def test_html_attachment_source_counts_css_before_recursive_assets(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "figure.png").write_bytes(b"PNG")
    (assets_dir / "style.css").write_text(
        "body { background: url(figure.png); }", encoding="utf-8"
    )
    source.write_text(
        '<html><head><link rel="stylesheet" href="article_assets/style.css"></head></html>',
        encoding="utf-8",
    )

    attachment_source, report = _html_attachment_source_with_embedded_assets(
        source, max_assets=1
    )

    assert attachment_source == source
    assert report["failed"] is True
    assert report["reason"] == "unresolved_local_assets"
    assert report["embedded_stylesheets"] == 1
    assert report["embedded_assets"] == 0
    assert report["missing_local_refs"] == ["figure.png"]
    assert report["unresolved_local_refs"] == ["figure.png"]
    assert report["skipped_assets"][0]["reason"] == "asset_count_limit"


def test_full_text_attach_returns_none_without_relay(tmp_path: Path) -> None:
    source = tmp_path / "article.html"
    source.write_text("<html></html>", encoding="utf-8")
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(zotero_relay_url="")

    result = processor._attach_full_text_result(
        attachment=SimpleNamespace(storage_dir=tmp_path / "storage"),
        metadata=SimpleNamespace(title="Article"),
        inventory={"attachments": []},
        payload={
            "html_downloads": [{"ok": True, "output_path": str(source)}],
            "pdf_downloads": [],
        },
    )

    assert result is None


def test_full_text_attach_reports_missing_html_source(tmp_path: Path) -> None:
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(zotero_relay_url="http://relay")
    missing = tmp_path / "missing.html"

    result = processor._attach_full_text_result(
        attachment=SimpleNamespace(storage_dir=tmp_path / "storage"),
        metadata=SimpleNamespace(title="Article"),
        inventory={"attachments": []},
        payload={
            "html_downloads": [
                {
                    "ok": True,
                    "output_path": str(missing),
                    "article": _valid_full_article_assessment(),
                }
            ],
            "pdf_downloads": [],
        },
    )

    assert result == {
        "ok": False,
        "status": "local_source_missing",
        "sourcePath": str(missing),
    }


def test_full_text_attach_html_without_assets_sends_original_file(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    source.write_text("<html><body>Article</body></html>", encoding="utf-8")
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(zotero_relay_url="http://relay")
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    captured: dict[str, object] = {}

    def fake_create_parent_attachment_via_relay(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "ok": True,
            "newAttachmentKey": "HTML0001",
            "newAttachmentVersion": 1,
        }

    monkeypatch.setattr(
        processor,
        "_create_parent_attachment_via_relay",
        fake_create_parent_attachment_via_relay,
    )

    result = processor._attach_full_text_result(
        attachment=attachment,
        metadata=metadata,
        inventory={"attachments": []},
        payload={
            "html_downloads": [
                {
                    "ok": True,
                    "output_path": str(source),
                    "article": _valid_full_article_assessment(),
                }
            ],
            "pdf_downloads": [],
        },
    )

    assert result is not None
    assert result["kind"] == "html"
    relay_source = Path(captured["source_path"])  # type: ignore[arg-type]
    assert relay_source.name.startswith(".z2m-parent-attachment-snapshot-")
    assert relay_source.suffix == ".html"
    assert "article_packages" in relay_source.parts
    assert result["raw_source_path"] == str(source)
    assert captured["content_type"] == "text/html"
    assert result["embedded_assets"] == {
        "enabled": False,
        "reason": "assets_dir_missing",
    }
    attached_html = Path(result["local_copy"]["path"]).read_text(encoding="utf-8")
    assert 'id="web-doc"' in attached_html
    assert "Article" in attached_html
    assert result["article_standard"]["polish"]["kind"] == "unknown"


def test_full_text_attach_attaches_pdf_alongside_html(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    html = tmp_path / "article.html"
    html.write_text("<html><body>Article</body></html>", encoding="utf-8")
    pdf = tmp_path / "article.pdf"
    pdf.write_bytes(b"%PDF")
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(zotero_relay_url="http://relay")
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    calls: list[dict[str, object]] = []

    def fake_create_parent_attachment_via_relay(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        key = "HTML0002" if kwargs["content_type"] == "text/html" else "PDF0002"
        return {
            "ok": True,
            "newAttachmentKey": key,
            "newAttachmentVersion": 1,
        }

    monkeypatch.setattr(
        processor,
        "_create_parent_attachment_via_relay",
        fake_create_parent_attachment_via_relay,
    )

    result = processor._attach_full_text_result(
        attachment=attachment,
        metadata=metadata,
        inventory={"attachments": []},
        payload={
            "html_downloads": [
                {
                    "ok": True,
                    "output_path": str(html),
                    "article": _valid_full_article_assessment(),
                }
            ],
            "pdf_downloads": [
                {
                    "ok": True,
                    "output_path": str(pdf),
                    "status": "downloaded",
                    "identity": {"needs_ocr": False},
                }
            ],
        },
    )

    assert result is not None
    assert result["kind"] == "html"
    assert result["attached_kinds"] == ["html", "pdf"]
    assert result["pdf_attachment"]["kind"] == "pdf"
    assert [call["content_type"] for call in calls] == ["text/html", "application/pdf"]
    relay_html = Path(calls[0]["source_path"])  # type: ignore[arg-type]
    assert relay_html.name.startswith(".z2m-parent-attachment-snapshot-")
    assert relay_html.suffix == ".html"
    assert "article_packages" in relay_html.parts
    assert result["raw_source_path"] == str(html)
    relay_pdf = Path(calls[1]["source_path"])  # type: ignore[arg-type]
    assert relay_pdf.name.startswith(".z2m-parent-attachment-snapshot-")
    assert relay_pdf.suffix == ".pdf"


def test_full_text_attach_returns_none_when_no_download_succeeded(
    tmp_path: Path,
) -> None:
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(zotero_relay_url="http://relay")

    result = processor._attach_full_text_result(
        attachment=SimpleNamespace(storage_dir=tmp_path / "storage"),
        metadata=SimpleNamespace(title="Article"),
        inventory={"attachments": []},
        payload={
            "html_downloads": [
                {"ok": False, "status": "title_mismatch", "output_path": ""}
            ],
            "pdf_downloads": [
                {"ok": False, "status": "identity_mismatch", "output_path": ""}
            ],
        },
    )

    assert result is None


def test_full_text_attach_pdf_needing_ocr_returns_downstream_reference(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "downloaded.pdf"
    source.write_bytes(b"%PDF")
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(zotero_relay_url="http://relay")
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Important Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")

    monkeypatch.setattr(
        processor,
        "_create_parent_attachment_via_relay",
        lambda **_kwargs: {
            "ok": True,
            "newAttachmentKey": "PDF9999",
            "newAttachmentVersion": 1,
        },
    )

    result = processor._attach_full_text_result(
        attachment=attachment,
        metadata=metadata,
        inventory={"attachments": []},
        payload={
            "html_downloads": [],
            "pdf_downloads": [
                {
                    "ok": True,
                    "status": "downloaded_needs_ocr",
                    "output_path": str(source),
                    "identity": {"needs_ocr": True},
                }
            ],
        },
    )

    assert result is not None
    assert result["kind"] == "pdf"
    assert result["ocr_enqueue"]["classification"] == "downstream_orchestrator"
    assert result["ocr_enqueue"]["stage"] == "ocr"
    assert result["ocr_enqueue"]["attachment"]["key"] == "PDF9999"
    assert Path(result["local_copy"]["path"]).exists()


def test_full_text_attach_pdf_with_text_returns_downstream_html_reference(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "downloaded.pdf"
    source.write_bytes(b"%PDF")
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(zotero_relay_url="http://relay")
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Important Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")

    monkeypatch.setattr(
        processor,
        "_create_parent_attachment_via_relay",
        lambda **_kwargs: {
            "ok": True,
            "newAttachmentKey": "PDF8888",
            "newAttachmentVersion": 1,
        },
    )

    result = processor._attach_full_text_result(
        attachment=attachment,
        metadata=metadata,
        inventory={"attachments": []},
        payload={
            "html_downloads": [],
            "pdf_downloads": [
                {
                    "ok": True,
                    "status": "downloaded",
                    "output_path": str(source),
                    "identity": {"needs_ocr": False},
                }
            ],
        },
    )

    assert result is not None
    assert result["kind"] == "pdf"
    assert result["html_enqueue"]["classification"] == "downstream_orchestrator"
    assert result["html_enqueue"]["stage"] == "pdf_html"
    assert result["html_enqueue"]["attachment"]["key"] == "PDF8888"


def test_full_text_html_selection_prefers_full_arxiv_article() -> None:
    landing = {
        "source": "zotero_translation_server",
        "url": "https://arxiv.org/abs/2511.02824",
        "kind": "landing",
        "ok": True,
        "output_path": "/tmp/abs.html",
        "article": {
            "text_chars": 6138,
            "markers": ["citation_title", "abstract", "references", "arxiv_html"],
            "section_markers": ["abstract", "references"],
        },
    }
    full_article = {
        "source": "arxiv",
        "url": "https://arxiv.org/html/2511.02824",
        "kind": "html",
        "ok": True,
        "output_path": "/tmp/full.html",
        "article": {
            "text_chars": 147180,
            "markers": ["article_tag", "arxiv_ltx_document", "arxiv_ltx_bibliography"],
            "section_markers": [
                "abstract",
                "methods",
                "results",
                "discussion",
                "references",
            ],
        },
    }

    assert _best_successful_html_download([landing, full_article]) is full_article


def test_full_text_html_selection_rejects_arxiv_abs_landing() -> None:
    landing = {
        "source": "zotero_translation_server",
        "url": "https://arxiv.org/abs/2511.02824",
        "final_url": "https://arxiv.org/abs/2511.02824",
        "kind": "landing",
        "ok": True,
        "output_path": "/tmp/abs.html",
        "article": {
            "text_chars": 6138,
            "markers": ["citation_title", "abstract", "references", "arxiv_html"],
            "section_markers": ["abstract", "references"],
        },
    }

    assert _best_successful_html_download([landing]) is None


def _write_full_text_fixture(tmp_path: Path, *, with_pdf: bool) -> Path:
    data_dir = tmp_path / "Zotero_Test_Data"
    (data_dir / "storage").mkdir(parents=True)
    sqlite_path = data_dir / "zotero.sqlite"
    connection = sqlite3.connect(sqlite_path)
    try:
        connection.executescript(
            """
            create table itemTypes (itemTypeID integer primary key, typeName text);
            create table items (
                itemID integer primary key,
                itemTypeID integer,
                dateModified text,
                key text,
                version integer
            );
            create table deletedItems (itemID integer primary key);
            create table itemAttachments (
                itemID integer primary key,
                parentItemID integer,
                linkMode integer,
                contentType text,
                path text
            );
            create table fields (fieldID integer primary key, fieldName text);
            create table itemDataValues (valueID integer primary key, value text);
            create table itemData (itemID integer, fieldID integer, valueID integer);
            create table creatorTypes (creatorTypeID integer primary key, creatorType text);
            create table creators (creatorID integer primary key, firstName text, lastName text, fieldMode integer);
            create table itemCreators (itemID integer, creatorID integer, creatorTypeID integer, orderIndex integer);
            create table tags (tagID integer primary key, name text);
            create table itemTags (itemID integer, tagID integer);
            create table collections (collectionID integer primary key, key text, collectionName text);
            create table collectionItems (collectionID integer, itemID integer);
            create table relations (subject integer, predicate text, object text);
            insert into itemTypes values (1, 'journalArticle'), (2, 'attachment');
            insert into items values (10, 1, '2026-01-01', 'PARENT1', 5);
            insert into fields values (1, 'title'), (2, 'DOI');
            insert into itemDataValues values (1, 'A Careful Metadata Pipeline'), (2, '10.1000/example');
            insert into itemData values (10, 1, 1), (10, 2, 2);
            """
        )
        if with_pdf:
            connection.executescript(
                """
                insert into items values (20, 2, '2026-01-02', 'PDF1234', 1);
                insert into itemAttachments values (20, 10, 0, 'application/pdf', 'storage:paper.pdf');
                """
            )
        connection.commit()
    finally:
        connection.close()
    return data_dir


def _add_full_text_html_attachment(
    data_dir: Path,
    *,
    filename: str = "article [SOURCE HTML].html",
    key: str = "HTML1234",
) -> None:
    connection = sqlite3.connect(data_dir / "zotero.sqlite")
    try:
        connection.execute(
            "insert into items values (21, 2, '2026-01-03', ?, 2)",
            (key,),
        )
        connection.execute(
            "insert into itemAttachments values (21, 10, 0, 'text/html', ?)",
            (f"storage:{filename}",),
        )
        connection.commit()
    finally:
        connection.close()
    (data_dir / "storage" / key).mkdir(parents=True)
    (data_dir / "storage" / key / filename).write_text(
        "<html><body>Article</body></html>", encoding="utf-8"
    )


def _add_second_parent(
    data_dir: Path,
    *,
    key: str,
    title: str,
    doi: str,
) -> None:
    connection = sqlite3.connect(data_dir / "zotero.sqlite")
    try:
        connection.execute(
            "insert into items values (30, 1, '2026-01-04', ?, 6)",
            (key,),
        )
        connection.executemany(
            "insert into itemDataValues values (?, ?)",
            [(30, title), (31, doi)],
        )
        connection.executemany(
            "insert into itemData values (30, ?, ?)",
            [(1, 30), (2, 31)],
        )
        connection.commit()
    finally:
        connection.close()


def _metadata_processor_test_config(tmp_path: Path, data_dir: Path) -> SimpleNamespace:
    storage_dir = data_dir / "storage"
    return SimpleNamespace(
        zotero_data_dir=data_dir,
        zotero_data_dirs=(data_dir,),
        zotero_sqlite_path=data_dir / "zotero.sqlite",
        resolved_storage_dir=storage_dir,
        state_db_path=tmp_path / "state.sqlite",
        ingest_data_root=tmp_path / "ingest",
        html_data_root=tmp_path / "html",
        validate_for_scan=lambda: None,
        metadata_job_lease_seconds=60,
        zotero_translation_server_url="",
        zotero_translation_server_timeout_seconds=30,
        metadata_crossref_email="",
        metadata_unpaywall_email="",
        metadata_openalex_api_key="",
        metadata_semantic_scholar_api_key="",
        metadata_core_api_key="",
        metadata_request_timeout_seconds=30,
        metadata_user_agent="test-agent",
        metadata_title_min_score=0.86,
        arxiv_search_min_score=0.88,
        metadata_policy="emptyFieldsOnly",
        metadata_extended_providers_enabled=False,
        scihub_enabled=True,
        scihub_mirrors=("https://sci-hub.test/",),
        scihub_user_agent="test-agent",
        scihub_request_timeout_seconds=1,
    )


def test_html_attachment_source_rewrites_srcset_local_url_containing_comma(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    figure = assets_dir / "iiif" / "full" / "1234," / "0" / "default.jpg"
    figure.parent.mkdir(parents=True)
    figure.write_bytes(b"JPEG")
    existing_data_uri = "data:image/gif;base64,R0lGODlhAQABAIAAAAUEBA=="
    source.write_text(
        (
            '<html><picture><source srcset="'
            "article_assets/iiif/full/1234,/0/default.jpg 1x, "
            f'{existing_data_uri} 2x"></picture></html>'
        ),
        encoding="utf-8",
    )

    embedded, report = _html_attachment_source_with_embedded_assets(source)

    assert report["enabled"] is True
    saved = embedded.read_text(encoding="utf-8")
    assert "article_assets/iiif/full/1234,/0/default.jpg" not in saved
    assert existing_data_uri in saved
    assert saved.count("data:image/") == 2


def test_html_attachment_source_rewrites_namespaced_svg_resource_tags(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "figure.png").write_bytes(b"PNG")
    (assets_dir / "symbols.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><g id="mark"/></svg>',
        encoding="utf-8",
    )
    source.write_text(
        (
            "<html><svg:svg>"
            '<svg:image xlink:href="article_assets/figure.png"></svg:image>'
            '<svg:use href="article_assets/symbols.svg#mark"></svg:use>'
            "</svg:svg></html>"
        ),
        encoding="utf-8",
    )

    embedded, report = _html_attachment_source_with_embedded_assets(source)

    assert report["enabled"] is True
    saved = embedded.read_text(encoding="utf-8")
    assert "article_assets/figure.png" not in saved
    assert "article_assets/symbols.svg" not in saved
    assert "data:image/png;base64," in saved
    assert "data:image/svg+xml;base64," in saved
    assert "#mark" in saved


def test_html_attachment_source_preserves_conditional_css_import_semantics(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "base sheet.css").write_text(
        ".grid{display:grid}",
        encoding="utf-8",
    )
    (assets_dir / "style.css").write_text(
        (
            '@import "base sheet.css" layer(base) supports(display: grid) '
            "screen and (min-width: 10px);"
        ),
        encoding="utf-8",
    )
    source.write_text(
        '<html><head><link rel="stylesheet" href="article_assets/style.css"></head></html>',
        encoding="utf-8",
    )

    embedded, report = _html_attachment_source_with_embedded_assets(source)

    assert report["enabled"] is True
    saved = embedded.read_text(encoding="utf-8")
    assert "@import" not in saved
    assert "@layer base" in saved
    assert "@supports (display: grid)" in saved
    assert "@media screen and (min-width: 10px)" in saved
    assert ".grid{display:grid}" in saved


def test_local_asset_candidate_scan_counts_directories_toward_limit(
    tmp_path: Path,
) -> None:
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "a").mkdir()
    (assets_dir / "b").mkdir()
    (assets_dir / "c").mkdir()

    candidates, truncated = full_text_attachment_module._local_asset_candidates(
        assets_dir,
        max_scanned_assets=2,
    )

    assert candidates == []
    assert truncated is True


def test_html_attachment_source_removes_owned_snapshot_when_validation_fails(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "figure.png").write_bytes(b"PNG")
    source.write_text(
        '<html><body><img src="article_assets/figure.png"></body></html>',
        encoding="utf-8",
    )
    original_fingerprint = full_text_attachment_module._stable_file_fingerprint

    def fail_published_snapshot(path: Path, *, max_bytes: int) -> object:
        if path.name.startswith("z2m_embedded."):
            raise OSError("published snapshot unreadable")
        return original_fingerprint(path, max_bytes=max_bytes)

    monkeypatch.setattr(
        full_text_attachment_module,
        "_stable_file_fingerprint",
        fail_published_snapshot,
    )

    attachment_source, report = _html_attachment_source_with_embedded_assets(source)

    assert attachment_source == source
    assert report["failed"] is True
    assert report["reason"] == "output_publish_failed"
    assert "published snapshot unreadable" in report["error"]
    assert not list(tmp_path.glob("z2m_embedded.*.html"))
    assert not list(tmp_path.glob("*.z2m-embedded-tmp-*"))


def test_html_attachment_publish_cleanup_does_not_mask_write_failure(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "figure.png").write_bytes(b"PNG")
    source.write_text(
        '<html><body><img src="article_assets/figure.png"></body></html>',
        encoding="utf-8",
    )
    original_unlink = Path.unlink

    def write_then_fail(path: Path, _text: str, *, max_bytes: int) -> object:
        assert max_bytes > 0
        path.write_bytes(b"partial")
        raise OSError("snapshot write failed")

    def fail_temp_cleanup(path: Path, *args: object, **kwargs: object) -> None:
        if path.name.startswith(".z2m-embedded-tmp-"):
            raise PermissionError("cleanup denied")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(
        full_text_attachment_module,
        "_write_text_file_bounded",
        write_then_fail,
    )
    monkeypatch.setattr(Path, "unlink", fail_temp_cleanup)

    attachment_source, report = _html_attachment_source_with_embedded_assets(source)

    assert attachment_source == source
    assert report["failed"] is True
    assert report["reason"] == "output_publish_failed"
    assert "snapshot write failed" in report["error"]


def test_html_attachment_source_concurrent_repair_of_corrupt_cache_is_exact(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "figure.png").write_bytes(b"PNG")
    source.write_text(
        '<html><body><img src="article_assets/figure.png"></body></html>',
        encoding="utf-8",
    )
    embedded, initial_report = _html_attachment_source_with_embedded_assets(source)
    expected_bytes = embedded.read_bytes()
    embedded.write_bytes(b"corrupt-cache")

    worker_count = 6
    barrier = threading.Barrier(worker_count)
    original_repair = full_text_attachment_module._link_or_repair_embedded_snapshot

    def synchronized_repair(
        temp_path: Path,
        target_path: Path,
        *,
        expected: object,
        max_bytes: int,
    ) -> bool:
        barrier.wait(timeout=5)
        return original_repair(
            temp_path,
            target_path,
            expected=expected,  # type: ignore[arg-type]
            max_bytes=max_bytes,
        )

    monkeypatch.setattr(
        full_text_attachment_module,
        "_link_or_repair_embedded_snapshot",
        synchronized_repair,
    )
    outcomes: list[tuple[Path, dict[str, Any]]] = []
    errors: list[BaseException] = []
    result_lock = threading.Lock()

    def run() -> None:
        try:
            result = _html_attachment_source_with_embedded_assets(source)
        except BaseException as exc:
            with result_lock:
                errors.append(exc)
        else:
            with result_lock:
                outcomes.append(result)

    workers = [threading.Thread(target=run) for _ in range(worker_count)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    assert len(outcomes) == worker_count
    unexpected_reports = [report for path, report in outcomes if path != embedded]
    assert unexpected_reports == [], [
        (report.get("reason"), report.get("error")) for report in unexpected_reports
    ]
    assert all(report["enabled"] is True for _path, report in outcomes)
    assert any(report["cache_reused"] is False for _path, report in outcomes)
    assert all(
        report["output"] == initial_report["output"] for _path, report in outcomes
    )
    assert embedded.read_bytes() == expected_bytes
    assert not list(tmp_path.glob("*.z2m-embedded-tmp-*"))


def test_html_attachment_source_retains_snapshot_after_post_publish_source_mutation(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "figure.png").write_bytes(b"PNG")
    source.write_text(
        '<html><body><img src="article_assets/figure.png"></body></html>',
        encoding="utf-8",
    )
    original_publish = full_text_attachment_module._publish_embedded_html_snapshot

    def publish_then_mutate(
        source_path: Path,
        html_text: str,
        *,
        max_bytes: int,
    ) -> tuple[Path, object, bool]:
        published_path, fingerprint, cache_reused = original_publish(
            source_path,
            html_text,
            max_bytes=max_bytes,
        )
        source.write_text("<html><body>changed</body></html>", encoding="utf-8")
        return published_path, fingerprint, cache_reused

    monkeypatch.setattr(
        full_text_attachment_module,
        "_publish_embedded_html_snapshot",
        publish_then_mutate,
    )

    attachment_source, report = _html_attachment_source_with_embedded_assets(source)

    assert attachment_source == source
    assert report["failed"] is True
    assert report["reason"] == "source_changed"
    snapshots = list(tmp_path.glob("z2m_embedded.*.html"))
    assert len(snapshots) == 1
    assert "data:image/png;base64," in snapshots[0].read_text(encoding="utf-8")
    assert not list(tmp_path.glob("*.z2m-embedded-tmp-*"))


def test_html_attachment_source_fails_closed_when_asset_scan_is_truncated(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "figure.png").write_bytes(b"PNG")
    (assets_dir / "unused-a").mkdir()
    (assets_dir / "unused-b").mkdir()
    source.write_text(
        '<html><body><img src="article_assets/figure.png"></body></html>',
        encoding="utf-8",
    )

    attachment_source, report = _html_attachment_source_with_embedded_assets(
        source,
        max_scanned_assets=1,
    )

    assert attachment_source == source
    assert report["enabled"] is False
    assert report["failed"] is True
    assert report["reason"] == "asset_scan_limit"
    assert report["max_scanned_assets"] == 1
    assert not list(tmp_path.glob("z2m_embedded.*.html"))


def test_full_text_drain_persists_attachment_failure_instead_of_succeeding(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    data_dir = _write_full_text_fixture(tmp_path, with_pdf=False)
    config = _metadata_processor_test_config(tmp_path, data_dir)
    config.zotero_relay_url = "http://relay.test"
    processor = ZoteroMetadataProcessor(config)
    processor._library_configs = lambda **_kwargs: [config]  # type: ignore[method-assign]
    queued = processor.full_text_backlog_scan(limit=1)
    assert queued["queued"] == 1

    class FakeFullTextResult:
        status = "html_found"

        def to_dict(self) -> dict[str, Any]:
            return {
                "status": self.status,
                "html_downloads": [
                    {
                        "ok": True,
                        "output_path": str(tmp_path / "found.html"),
                        "article": _valid_full_article_assessment(),
                    }
                ],
                "pdf_downloads": [],
            }

    monkeypatch.setattr(
        "zotero_ingest_worker.metadata_processor.discover_and_download_full_text",
        lambda **_kwargs: FakeFullTextResult(),
    )
    attachment_failure = {
        "ok": False,
        "kind": "html",
        "status": "local_copy_failed",
        "relay": {"ok": True, "newAttachmentKey": "HTMLFAIL"},
        "local_copy": {
            "ok": False,
            "reason": "local_copy_failed",
            "error": "PermissionError: storage is locked",
        },
    }
    monkeypatch.setattr(
        processor,
        "_attach_full_text_result",
        lambda **_kwargs: attachment_failure,
    )

    drained = processor.drain_full_text_queue(limit=1)

    assert drained["ok"] is False
    assert drained["processed"] == 1
    assert drained["failed"] == 1
    job = drained["results"][0]
    assert job["status"] == "failed_retryable"
    assert job["relay_status"] == "succeeded"
    assert "local_copy_failed" in job["last_error"]
    stored = json.loads(str(job["result_json"]))
    assert stored["worker_status"] == "html_found"
    assert stored["relay_attachment"] == attachment_failure
    persisted = processor.state.get_metadata_job(str(job["job_id"]))
    assert persisted is not None
    assert persisted["status"] == "failed_retryable"
    assert (
        json.loads(str(persisted["result_json"]))["relay_attachment"]
        == attachment_failure
    )


def test_html_attachment_source_reports_css_asset_resolution_failure(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    stylesheet = assets_dir / "style.css"
    stylesheet.write_text(
        '.figure{background-image:url("broken.png")}',
        encoding="utf-8",
    )
    broken_asset = assets_dir / "broken.png"
    broken_asset.write_bytes(b"PNG")
    source.write_text(
        '<html><head><link rel="stylesheet" href="article_assets/style.css"></head></html>',
        encoding="utf-8",
    )
    original_resolve = Path.resolve

    def fail_broken_asset_resolve(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> Path:
        if path.name == "broken.png":
            raise OSError("filesystem resolution failed")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail_broken_asset_resolve)

    attachment_source, report = _html_attachment_source_with_embedded_assets(source)

    assert attachment_source == source
    assert report["enabled"] is False
    assert report["failed"] is True
    assert report["reason"] == "unresolved_local_assets"
    assert report["unresolved_local_refs"] == ["broken.png"]
    assert report["missing_local_refs"] == ["broken.png"]
    assert not list(tmp_path.glob("z2m_embedded.*.html"))


@pytest.mark.parametrize(
    "local_url",
    ["file:///C:/host-secret.png", r"C:\host-secret.png"],
    ids=["file-scheme", "windows-drive"],
)
def test_html_attachment_source_rejects_absolute_local_resource_urls(
    tmp_path: Path,
    local_url: str,
) -> None:
    source = tmp_path / "article.html"
    source.write_text(
        f'<html><body><img src="{local_url}"></body></html>',
        encoding="utf-8",
    )

    attachment_source, report = _html_attachment_source_with_embedded_assets(source)

    assert attachment_source == source
    assert report["enabled"] is False
    assert report["failed"] is True
    assert report["reason"] == "unresolved_local_assets"
    assert report["unresolved_local_refs"] == [local_url]


def test_html_attachment_source_escapes_case_insensitive_style_end_tag(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "style.css").write_text(
        '.label::before{content:"</STYLE><img src=missing.png>"}',
        encoding="utf-8",
    )
    source.write_text(
        '<html><head><link rel="stylesheet" href="article_assets/style.css"></head></html>',
        encoding="utf-8",
    )

    attachment_source, report = _html_attachment_source_with_embedded_assets(source)

    assert report["enabled"] is True
    assert report.get("failed") is not True
    assert attachment_source != source
    embedded = attachment_source.read_text(encoding="utf-8")
    assert "<\\/STYLE><img src=missing.png>" in embedded
    assert embedded.count("</style>") == 1


def test_html_attachment_source_preserves_stylesheet_media_attribute(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "print.css").write_text("body{color:black}", encoding="utf-8")
    source.write_text(
        '<html><head><link rel="stylesheet" media="print" '
        'href="article_assets/print.css"></head></html>',
        encoding="utf-8",
    )

    attachment_source, report = _html_attachment_source_with_embedded_assets(source)

    assert report["enabled"] is True
    embedded = attachment_source.read_text(encoding="utf-8")
    assert '<style media="print">' in embedded
    assert "body{color:black}" in embedded


def test_html_attachment_source_fails_closed_for_disabled_stylesheet(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "disabled.css").write_text("body{color:red}", encoding="utf-8")
    source.write_text(
        '<html><head><link rel="stylesheet" disabled '
        'href="article_assets/disabled.css"></head></html>',
        encoding="utf-8",
    )

    attachment_source, report = _html_attachment_source_with_embedded_assets(source)

    assert attachment_source == source
    assert report["enabled"] is False
    assert report["failed"] is True
    assert report["reason"] == "unresolved_local_assets"
    assert report["unresolved_local_refs"] == ["article_assets/disabled.css"]


def test_html_attachment_source_reports_asset_root_resolution_failure(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "figure.png").write_bytes(b"PNG")
    source.write_text(
        '<html><body><img src="article_assets/figure.png"></body></html>',
        encoding="utf-8",
    )
    original_resolve = Path.resolve

    def fail_asset_root_resolve(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> Path:
        if path == assets_dir:
            raise OSError("asset root resolution failed")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail_asset_root_resolve)

    attachment_source, report = _html_attachment_source_with_embedded_assets(source)

    assert attachment_source == source
    assert report["enabled"] is False
    assert report["failed"] is True
    assert report["reason"] == "asset_root_unstable"
    assert "asset root resolution failed" in report["error"]
    assert not list(tmp_path.glob("z2m_embedded.*.html"))


def test_full_text_drain_persists_real_relay_exception_as_attachment_failure(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    data_dir = _write_full_text_fixture(tmp_path, with_pdf=False)
    config = _metadata_processor_test_config(tmp_path, data_dir)
    config.zotero_relay_url = "http://relay.test"
    processor = ZoteroMetadataProcessor(config)
    processor._library_configs = lambda **_kwargs: [config]  # type: ignore[method-assign]
    queued = processor.full_text_backlog_scan(limit=1)
    assert queued["queued"] == 1
    found_html = tmp_path / "found.html"
    body = " ".join(
        [
            "This complete article paragraph contains methods, results, "
            "evidence, references, and discussion."
        ]
        * 100
    )
    found_html.write_text(
        "<html><head><title>A Careful Metadata Pipeline</title></head>"
        f"<body><article><h1>A Careful Metadata Pipeline</h1><p>{body}</p>"
        "</article></body></html>",
        encoding="utf-8",
    )

    class FakeFullTextResult:
        status = "html_found"

        def to_dict(self) -> dict[str, Any]:
            return {
                "status": self.status,
                "html_downloads": [
                    {
                        "ok": True,
                        "output_path": str(found_html),
                        "article": _valid_full_article_assessment(),
                    }
                ],
                "pdf_downloads": [],
            }

    monkeypatch.setattr(
        "zotero_ingest_worker.metadata_processor.discover_and_download_full_text",
        lambda **_kwargs: FakeFullTextResult(),
    )

    def fail_relay(**_kwargs: Any) -> dict[str, Any]:
        raise ConnectionError("relay connection reset during attachment")

    monkeypatch.setattr(processor, "_create_parent_attachment_via_relay", fail_relay)

    drained = processor.drain_full_text_queue(limit=1)

    assert drained["ok"] is False
    assert drained["processed"] == 1
    assert drained["failed"] == 1
    job = drained["results"][0]
    assert job["status"] == "failed_retryable"
    assert job["relay_status"] == "failed"
    assert "relay_attachment_failed" in job["last_error"]
    payload = json.loads(str(job["result_json"]))
    attachment_failure = payload["relay_attachment"]
    assert attachment_failure["ok"] is False
    assert attachment_failure["status"] == "relay_attachment_failed"
    assert attachment_failure["relay"]["reason"] == "relay_attachment_failed"
    assert (
        "relay connection reset during attachment"
        in attachment_failure["relay"]["error"]
    )
    persisted = processor.state.get_metadata_job(str(job["job_id"]))
    assert persisted is not None
    assert persisted["status"] == "failed_retryable"
    persisted_payload = json.loads(str(persisted["result_json"]))
    assert persisted_payload["relay_attachment"] == attachment_failure


def test_full_text_drain_requires_relay_before_leasing_jobs(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    data_dir = _write_full_text_fixture(tmp_path, with_pdf=False)
    config = _metadata_processor_test_config(tmp_path, data_dir)
    config.zotero_relay_url = ""
    processor = ZoteroMetadataProcessor(config)
    processor._library_configs = lambda **_kwargs: [config]  # type: ignore[method-assign]
    queued = processor.full_text_backlog_scan(limit=1)
    assert queued["queued"] == 1

    def fail_if_leased(**_kwargs: Any) -> None:
        raise AssertionError("full-text job must not be leased without relay")

    monkeypatch.setattr(
        processor.state,
        "lease_next_metadata_job",
        fail_if_leased,
    )

    result = processor.drain_full_text_queue(limit=1)

    assert result["ok"] is False
    assert "ZOTERO_RELAY_URL is required" in result["error"]
    assert result["queue"]["queued"] == 1
    persisted = processor.state.list_metadata_jobs(
        job_type=METADATA_JOB_FULL_TEXT,
        statuses={"queued"},
        limit=10,
    )
    assert len(persisted) == 1


def test_html_attachment_source_reports_asset_scan_failure(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "figure.png").write_bytes(b"PNG")
    source.write_text(
        '<html><body><img src="article_assets/figure.png"></body></html>',
        encoding="utf-8",
    )

    def fail_scan(*_args: Any, **_kwargs: Any) -> tuple[list[Path], bool]:
        raise OSError("asset directory changed during scan")

    monkeypatch.setattr(
        full_text_attachment_module,
        "_local_asset_candidates",
        fail_scan,
    )

    attachment_source, report = _html_attachment_source_with_embedded_assets(source)

    assert attachment_source == source
    assert report["failed"] is True
    assert report["reason"] == "asset_scan_failed"
    assert "asset directory changed" in report["error"]


def test_html_attachment_source_rewrites_link_imagesrcset(tmp_path: Path) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "figure-1x.png").write_bytes(b"PNG-1X")
    (assets_dir / "figure-2x.png").write_bytes(b"PNG-2X")
    source.write_text(
        (
            '<html><head><link rel="preload" as="image" '
            'imagesrcset="article_assets/figure-1x.png 1x, '
            'article_assets/figure-2x.png 2x"></head><body></body></html>'
        ),
        encoding="utf-8",
    )

    attachment_source, report = _html_attachment_source_with_embedded_assets(source)

    assert report["enabled"] is True
    rendered = attachment_source.read_text(encoding="utf-8")
    assert rendered.count("data:image/png;base64,") == 2
    assert "article_assets/" not in rendered


def test_html_attachment_source_rewrites_css_url_with_intervening_comment(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "hero.png").write_bytes(b"PNG")
    source.write_text(
        (
            "<html><head><style>"
            ".hero { background-image: url/**/(article_assets/hero.png); }"
            "</style></head><body></body></html>"
        ),
        encoding="utf-8",
    )

    attachment_source, report = _html_attachment_source_with_embedded_assets(source)

    assert report["enabled"] is True
    rendered = attachment_source.read_text(encoding="utf-8")
    assert "data:image/png;base64," in rendered
    assert "article_assets/hero.png" not in rendered


def test_html_attachment_source_rewrites_escaped_css_url_identifier(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "hero.png").write_bytes(b"PNG")
    source.write_text(
        (
            "<html><head><style>"
            r".hero { background-image: u\72l(article_assets/hero.png); }"
            "</style></head><body></body></html>"
        ),
        encoding="utf-8",
    )

    attachment_source, report = _html_attachment_source_with_embedded_assets(source)

    assert report["enabled"] is True
    rendered = attachment_source.read_text(encoding="utf-8")
    assert "data:image/png;base64," in rendered
    assert r"u\72l(" not in rendered
    assert "article_assets/hero.png" not in rendered


def test_html_attachment_source_rewrites_escaped_css_url_path(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "hero.png").write_bytes(b"PNG")
    source.write_text(
        (
            "<html><head><style>"
            r".hero { background-image: url(article_assets/\68 ero.png); }"
            "</style></head><body></body></html>"
        ),
        encoding="utf-8",
    )

    attachment_source, report = _html_attachment_source_with_embedded_assets(source)

    assert report["enabled"] is True
    rendered = attachment_source.read_text(encoding="utf-8")
    assert "data:image/png;base64," in rendered
    assert r"article_assets/\68 ero.png" not in rendered


def test_html_attachment_source_inlines_escaped_css_import_keyword(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "hero.png").write_bytes(b"PNG")
    (assets_dir / "nested.css").write_text(
        ".hero { background-image: url(hero.png); }",
        encoding="utf-8",
    )
    (assets_dir / "main.css").write_text(
        r'@\69mport "nested.css";',
        encoding="utf-8",
    )
    source.write_text(
        (
            '<html><head><link rel="stylesheet" '
            'href="article_assets/main.css"></head><body></body></html>'
        ),
        encoding="utf-8",
    )

    attachment_source, report = _html_attachment_source_with_embedded_assets(source)

    assert report["enabled"] is True
    rendered = attachment_source.read_text(encoding="utf-8")
    assert "data:image/png;base64," in rendered
    assert r"@\69mport" not in rendered
    assert "nested.css" not in rendered


@pytest.mark.parametrize(
    ("relay_result", "local_result", "expected_reason"),
    [
        (
            {"ok": "true", "appliedFields": ["title", "DOI"], "newVersion": 8},
            None,
            "relay_metadata_patch_invalid_result",
        ),
        (
            {"ok": True, "appliedFields": ["title", "DOI"], "newVersion": 8},
            {"ok": "true"},
            "local_metadata_sync_invalid_result",
        ),
        (
            {"ok": True, "appliedFields": ["title", "DOI"], "newVersion": 8},
            {"ok": False, "reason": "sqlite_locked"},
            "local_metadata_sync_failed",
        ),
    ],
    ids=["relay-malformed", "local-malformed", "local-failed"],
)
def test_metadata_enrich_requires_exact_remote_and_local_publication(
    monkeypatch: Any,
    tmp_path: Path,
    relay_result: dict[str, object],
    local_result: dict[str, object] | None,
    expected_reason: str,
) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF")
    attachment = LocalAttachment(
        library_id="LIB1",
        data_dir=tmp_path,
        storage_dir=tmp_path / "storage",
        key="PDF1234",
        item_id=20,
        parent_item_id=10,
        date_modified=None,
        link_mode=0,
        content_type="application/pdf",
        zotero_path="storage:paper.pdf",
        file_path=pdf,
        parent_key="ITEM1234",
    )
    metadata = LocalItemMetadata(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        version=7,
        item_type="journalArticle",
        date_modified=None,
        fields={"title": "Paper"},
        creators=[],
        tags=[],
        collections=[],
        relations=[],
    )
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(zotero_relay_url="http://relay")
    processor._provider_events = []
    captured: dict[str, Any] = {}

    class FakeState:
        def mark_metadata_job_succeeded(self, **kwargs: Any) -> dict[str, Any]:
            captured["succeeded"] = kwargs
            return {"status": "succeeded", **kwargs}

        def mark_metadata_job_skipped(self, **kwargs: Any) -> dict[str, Any]:
            captured["skipped"] = kwargs
            return {"status": "skipped", **kwargs}

        def mark_metadata_job_failed(self, **kwargs: Any) -> dict[str, Any]:
            captured["failed"] = kwargs
            return {"status": "failed_retryable", **kwargs}

    processor.state = FakeState()
    monkeypatch.setattr(processor, "_attachment_for_job", lambda _job: attachment)
    monkeypatch.setattr(processor, "_config_for_job", lambda _job: processor.config)
    monkeypatch.setattr(
        metadata_processor_module,
        "LocalZoteroStore",
        lambda _config: SimpleNamespace(),
    )
    monkeypatch.setattr(
        processor,
        "_ensure_parent_metadata_context",
        lambda **_kwargs: (attachment, metadata, {"ok": True}),
    )
    monkeypatch.setattr(
        processor,
        "_lookup_metadata_candidate",
        lambda **_kwargs: MetadataCandidate(
            source="crossref",
            identifier="10.1000/example",
            score=1.0,
            fields={"title": "Better Paper", "DOI": "10.1000/example"},
            raw={},
        ),
    )
    monkeypatch.setattr(
        processor,
        "_patch_parent_metadata_via_relay",
        lambda **_kwargs: relay_result,
    )
    if local_result is not None:
        monkeypatch.setattr(
            metadata_processor_module,
            "sync_parent_metadata_local",
            lambda **_kwargs: local_result,
        )

    result = processor._drain_enrich_job(
        {"job_id": "job1"},
        require_relay=True,
        policy="emptyFieldsOnly",
    )

    assert result["status"] == "failed_retryable"
    assert "succeeded" not in captured
    assert captured["failed"]["result"]["reason"] == expected_reason
    assert captured["failed"]["relay_result"] == relay_result


def test_source_html_cleanup_aggregate_requires_exact_library_success(
    monkeypatch: Any,
) -> None:
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor._library_configs = lambda **_kwargs: [SimpleNamespace()]
    malformed_library_result = {
        "ok": "true",
        "scanned": 3,
        "affected_parents": 1,
        "candidate_count": 2,
    }
    monkeypatch.setattr(
        metadata_processor_module,
        "cleanup_source_html_library",
        lambda **_kwargs: malformed_library_result,
    )
    monkeypatch.setattr(
        metadata_processor_module,
        "LocalZoteroStore",
        lambda _config: SimpleNamespace(),
    )

    result = processor.source_html_cleanup(dry_run=True)

    assert result["ok"] is False
    assert result["libraries"][0]["ok"] is False
    assert result["libraries"][0]["reason"] == "invalid_source_html_cleanup_result"


def test_metadata_drain_heartbeat_recovers_after_transient_store_error(
    monkeypatch: Any,
) -> None:
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(metadata_job_lease_seconds=60)
    heartbeat_calls = {"count": 0}
    heartbeat_recovered = threading.Event()

    class FakeState:
        def heartbeat_metadata_job(self, **_kwargs: Any) -> bool:
            heartbeat_calls["count"] += 1
            if heartbeat_calls["count"] == 2:
                raise RuntimeError("temporary sqlite busy")
            if heartbeat_calls["count"] >= 3:
                heartbeat_recovered.set()
            return True

        def get_metadata_job(self, _job_id: str) -> dict[str, Any]:
            return {}

    processor.state = FakeState()
    monkeypatch.setattr(
        processor,
        "_metadata_job_heartbeat_interval_seconds",
        lambda _lease_seconds: 0.01,
    )

    def slow_handler(job: dict[str, Any]) -> dict[str, Any]:
        assert job["lease_owner"] == "owner-a"
        assert heartbeat_recovered.wait(timeout=2.0)
        return {"job_id": job["job_id"], "status": "succeeded"}

    monkeypatch.setattr(processor, "_drain_full_text_job", slow_handler)

    result = processor._drain_leased_job(
        job_type=METADATA_JOB_FULL_TEXT,
        job={
            "job_id": "job-heartbeat-retry",
            "lease_owner": "owner-a",
            "attempts": 1,
        },
        require_relay=False,
        policy=None,
    )

    assert result["status"] == "succeeded"
    assert heartbeat_calls["count"] >= 3


@pytest.mark.parametrize(
    "malformed_library_result",
    [
        None,
        [],
        {"ok": "true", "scanned": 3, "affected_parents": 1, "candidate_count": 2},
        {"ok": True, "scanned": "3", "affected_parents": 1, "candidate_count": 2},
        {"ok": True, "scanned": True, "affected_parents": 1, "candidate_count": 2},
        {"ok": True, "scanned": 3, "affected_parents": -1, "candidate_count": 2},
        {"ok": True, "scanned": 3, "affected_parents": 1, "candidate_count": False},
    ],
    ids=[
        "none",
        "list",
        "truthy-ok",
        "string-scanned",
        "boolean-scanned",
        "negative-affected",
        "boolean-candidates",
    ],
)
def test_source_html_cleanup_aggregate_rejects_malformed_library_contract(
    monkeypatch: Any,
    malformed_library_result: object,
) -> None:
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor._library_configs = lambda **_kwargs: [SimpleNamespace(library_id="LIB1")]
    monkeypatch.setattr(
        metadata_processor_module,
        "cleanup_source_html_library",
        lambda **_kwargs: malformed_library_result,
    )
    monkeypatch.setattr(
        metadata_processor_module,
        "LocalZoteroStore",
        lambda _config: SimpleNamespace(),
    )

    result = processor.source_html_cleanup(dry_run=True)

    assert result["ok"] is False
    assert result["scanned"] == 0
    assert result["affected_parents"] == 0
    assert result["candidate_count"] == 0
    assert result["libraries"][0]["ok"] is False
    assert result["libraries"][0]["reason"] == "invalid_source_html_cleanup_result"


def test_scihub_drain_grouped_job_continues_after_non_mapping_result(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    data_dir = _write_full_text_fixture(tmp_path, with_pdf=False)
    _add_full_text_html_attachment(data_dir)
    connection = sqlite3.connect(data_dir / "zotero.sqlite")
    try:
        connection.executescript(
            """
            insert into fields values (3, 'extra');
            insert into itemDataValues values (3, 'PMID: 31044789; PMCID: PMC1234567');
            insert into itemData values (10, 3, 3);
            """
        )
        connection.commit()
    finally:
        connection.close()
    config = _metadata_processor_test_config(tmp_path, data_dir)
    processor = ZoteroMetadataProcessor(config)
    processor._library_configs = lambda **_kwargs: [config]  # type: ignore[method-assign]
    processor.scihub_pdf_backlog_scan(limit=10)
    calls: list[str] = []

    def fake_download_and_attach_scihub_pdf(
        _config: Any,
        options: Any,
    ) -> object:
        calls.append(str(options.doi))
        if len(calls) == 1:
            return None
        return {
            "ok": True,
            "status": "attached",
            "download": {"ok": True, "output_path": str(tmp_path / "scihub.pdf")},
            "attach": {"ok": True},
        }

    monkeypatch.setattr(
        metadata_processor_module,
        "download_and_attach_scihub_pdf",
        fake_download_and_attach_scihub_pdf,
    )

    result = processor.drain_scihub_pdf_queue(limit=1, require_relay=False)

    assert result["ok"] is True
    assert len(calls) >= 2
    stored = json.loads(result["results"][0]["result_json"])
    assert stored["attempts"][0]["ok"] is False
    assert stored["attempts"][0]["status"] == "invalid_result"
    assert stored["attempts"][0]["result_type"] == "NoneType"


@pytest.mark.parametrize(
    "job",
    [
        {"attempts": "1", "max_attempts": 1},
        {"attempts": True, "max_attempts": 1},
        {"attempts": 1.0, "max_attempts": 1},
        {"attempts": 1, "max_attempts": "1"},
        {"attempts": 1, "max_attempts": True},
        {"attempts": 1, "max_attempts": 1.0},
        {"attempts": -1, "max_attempts": 1},
        {"attempts": 1, "max_attempts": -1},
    ],
    ids=[
        "string-attempts",
        "boolean-attempts",
        "float-attempts",
        "string-max",
        "boolean-max",
        "float-max",
        "negative-attempts",
        "negative-max",
    ],
)
def test_metadata_job_attempts_exhausted_requires_exact_nonnegative_integers(
    job: dict[str, object],
) -> None:
    assert metadata_processor_module._metadata_job_attempts_exhausted(job) is False


def test_metadata_job_attempts_exhausted_accepts_exact_attempt_budget() -> None:
    assert metadata_processor_module._metadata_job_attempts_exhausted(
        {"attempts": 2, "max_attempts": 2}
    )
    assert not metadata_processor_module._metadata_job_attempts_exhausted(
        {"attempts": 1, "max_attempts": 2}
    )


@pytest.mark.parametrize("lease_owner", [None, "", 17, True, [], {}])
def test_metadata_job_lease_owner_requires_nonempty_string(lease_owner: object) -> None:
    assert (
        metadata_processor_module._metadata_job_lease_owner(
            {"lease_owner": lease_owner}
        )
        is None
    )


def test_metadata_job_lease_owner_normalizes_string() -> None:
    assert (
        metadata_processor_module._metadata_job_lease_owner(
            {"lease_owner": "  owner-a  "}
        )
        == "owner-a"
    )


def test_metadata_drain_rejects_job_without_exact_lease_owner(
    monkeypatch: Any,
) -> None:
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(metadata_job_lease_seconds=60)

    class FakeState:
        def heartbeat_metadata_job(self, **_kwargs: Any) -> bool:
            return True

        def get_metadata_job(self, job_id: str) -> dict[str, Any]:
            return {"job_id": job_id, "status": "running", "lease_owner": 17}

    processor.state = FakeState()

    def forbidden_handler(_job: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError(
            "metadata handler must not run without an exact lease owner"
        )

    monkeypatch.setattr(processor, "_drain_full_text_job", forbidden_handler)

    result = processor._drain_leased_job(
        job_type=METADATA_JOB_FULL_TEXT,
        job={"job_id": "job-invalid-owner", "lease_owner": 17},
        require_relay=False,
        policy=None,
    )

    assert result["status"] == "running"
    assert result["invalid_lease"] is True


@pytest.mark.parametrize("attempt", [None, 0, True, 1.5, "1"])
def test_metadata_drain_rejects_invalid_attempt_token(
    monkeypatch: Any,
    attempt: object,
) -> None:
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(metadata_job_lease_seconds=60)

    class FakeState:
        def heartbeat_metadata_job(self, **_kwargs: Any) -> bool:
            raise AssertionError("invalid attempt must fail before heartbeat")

        def get_metadata_job(self, job_id: str) -> dict[str, Any]:
            return {"job_id": job_id, "status": "running", "lease_owner": "worker"}

    processor.state = FakeState()

    def forbidden_handler(_job: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("metadata handler must not run without an attempt token")

    monkeypatch.setattr(processor, "_drain_full_text_job", forbidden_handler)

    result = processor._drain_leased_job(
        job_type=METADATA_JOB_FULL_TEXT,
        job={
            "job_id": "job-invalid-attempt",
            "lease_owner": "worker",
            "attempts": attempt,
        },
        require_relay=False,
        policy=None,
    )

    assert result["status"] == "running"
    assert result["invalid_lease"] is True
    assert "attempt" in result["error"].lower()


def test_metadata_drain_cooperatively_stops_after_lease_loss(
    monkeypatch: Any,
) -> None:
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(metadata_job_lease_seconds=60)
    heartbeat_calls = {"count": 0}
    lease_lost = threading.Event()
    side_effects: list[str] = []

    class FakeState:
        def heartbeat_metadata_job(self, **_kwargs: Any) -> bool:
            heartbeat_calls["count"] += 1
            if heartbeat_calls["count"] >= 2:
                lease_lost.set()
                return False
            return True

        def get_metadata_job(self, job_id: str) -> dict[str, Any]:
            return {
                "job_id": job_id,
                "status": "queued",
                "lease_owner": None,
            }

        def mark_metadata_job_failed(self, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("lost lease must not write a terminal failure")

        def mark_metadata_job_skipped(self, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("lost lease must not write a terminal skip")

    processor.state = FakeState()
    monkeypatch.setattr(
        processor,
        "_metadata_job_heartbeat_interval_seconds",
        lambda _lease_seconds: 0.01,
    )

    def guarded_handler(job: dict[str, Any]) -> dict[str, Any]:
        assert lease_lost.wait(timeout=2.0)
        try:
            processor._ensure_metadata_job_lease_active()
        except Exception as exc:
            return processor._mark_metadata_job_failed_or_skipped_for_exception(
                job_id=str(job["job_id"]),
                exc=exc,
                job=job,
            )
        side_effects.append("published")
        return {"job_id": job["job_id"], "status": "succeeded"}

    monkeypatch.setattr(processor, "_drain_full_text_job", guarded_handler)

    result = processor._drain_leased_job(
        job_type=METADATA_JOB_FULL_TEXT,
        job={
            "job_id": "job-lost-lease",
            "lease_owner": "owner-a",
            "attempts": 1,
        },
        require_relay=False,
        policy=None,
    )

    assert result["status"] == "queued"
    assert result["stale_lease"] is True
    assert side_effects == []


def test_metadata_heartbeat_marks_lease_lost_after_state_outage_deadline(
    monkeypatch: Any,
) -> None:
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)

    class FailingState:
        def heartbeat_metadata_job(self, **_kwargs: Any) -> bool:
            raise RuntimeError("sqlite unavailable")

    processor.state = FailingState()
    monkeypatch.setattr(
        processor,
        "_metadata_job_heartbeat_interval_seconds",
        lambda _lease_seconds: 0.001,
    )
    monotonic_values = iter([100.0, 161.0])
    monkeypatch.setattr(
        metadata_processor_module.time,
        "monotonic",
        lambda: next(monotonic_values),
    )
    lease_lost = threading.Event()

    processor._heartbeat_metadata_job_until_stopped(
        job_id="job-state-outage",
        owner="owner-a",
        lease_seconds=60,
        stop=threading.Event(),
        lease_lost=lease_lost,
    )

    assert lease_lost.is_set()


def test_metadata_job_owner_is_unique_and_preserves_pid_suffix() -> None:
    owners = {metadata_processor_module.metadata_job_owner() for _ in range(8)}

    assert len(owners) == 8
    assert all(owner.rsplit(":", 1)[-1] == str(os.getpid()) for owner in owners)


def test_scihub_query_candidates_have_bounded_count_and_size(tmp_path: Path) -> None:
    metadata = LocalItemMetadata(
        library_id="LIB1",
        data_dir=tmp_path,
        key="PARENT1",
        item_id=1,
        version=1,
        item_type="journalArticle",
        date_modified=None,
        fields={
            "title": "Queue budget",
            "extra": " ".join(
                f"https://example.org/{index}/{'x' * 900}" for index in range(100)
            ),
        },
        creators=[],
        tags=[],
        collections=[],
        relations=[],
    )

    candidates = metadata_helpers_module._scihub_query_candidates(metadata)
    encoded = metadata_helpers_module._encode_scihub_query_candidates(candidates)

    assert len(candidates) <= metadata_helpers_module.MAX_SCIHUB_QUERY_CANDIDATES
    assert all(
        len(candidate["query"]) <= metadata_helpers_module.MAX_SCIHUB_QUERY_CHARS
        for candidate in candidates
    )
    assert (
        len(encoded.encode("utf-8"))
        <= metadata_helpers_module.MAX_SCIHUB_QUERY_LIST_BYTES
    )


def test_scihub_job_query_decoder_rejects_oversized_encoded_list() -> None:
    encoded = "doi:" + ("x" * (metadata_helpers_module.MAX_SCIHUB_QUERY_LIST_BYTES + 1))

    queries = metadata_helpers_module._scihub_queries_from_job(
        {"queue_key": f"v=scihub-pdf-legacy|query_list={encoded}"}
    )

    assert queries == []


def test_scihub_job_query_decoder_checks_utf8_budget_before_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded = "doi:" + (
        "é" * (metadata_helpers_module.MAX_SCIHUB_QUERY_LIST_BYTES // 2 + 1)
    )

    def fail_unquote(_value: str) -> str:
        raise AssertionError("oversized payload must be rejected before decoding")

    monkeypatch.setattr(metadata_helpers_module.urllib.parse, "unquote", fail_unquote)

    assert (
        metadata_helpers_module._scihub_queries_from_job(
            {"queue_key": f"v=scihub-pdf-legacy|query_list={encoded}"}
        )
        == []
    )


def test_scihub_job_query_decoder_accepts_exact_component_boundaries() -> None:
    query_type = "t" * metadata_helpers_module.MAX_SCIHUB_QUERY_TYPE_CHARS
    query = "q" * metadata_helpers_module.MAX_SCIHUB_QUERY_CHARS

    assert metadata_helpers_module._scihub_queries_from_job(
        {"queue_key": f"v=scihub-pdf-legacy|query_list={query_type}:{query}"}
    ) == [{"type": query_type, "query": query}]


def test_scihub_job_query_decoder_stops_at_queue_field_delimiter() -> None:
    assert metadata_helpers_module._scihub_queries_from_job(
        {
            "queue_key": (
                "v=scihub-pdf-legacy|query_list=doi:10.1000%2Ffirst"
                "|query=10.1000%2Fsecond"
            )
        }
    ) == [{"type": "doi", "query": "10.1000/first"}]


def test_scihub_candidate_bound_rejects_non_string_runtime_values() -> None:
    candidates: Any = [
        {"type": True, "query": "10.1/bool-type"},
        {"type": "doi", "query": 7},
        {"type": "doi", "query": "10.1/valid"},
    ]

    assert metadata_helpers_module._bounded_scihub_query_candidates(candidates) == [
        {"type": "doi", "query": "10.1/valid"}
    ]


def test_http_error_body_uses_bounded_read() -> None:
    read_sizes: list[int] = []

    class FakeHttpError:
        def read(self, size: int) -> bytes:
            read_sizes.append(size)
            return b"x" * size

        def __str__(self) -> str:
            return "HTTP 500"

    error: Any = FakeHttpError()

    body = metadata_helpers_module._http_error_body(error)

    assert read_sizes == [metadata_helpers_module.MAX_HTTP_ERROR_BODY_BYTES]
    assert body == "x" * metadata_helpers_module.MAX_HTTP_ERROR_BODY_CHARS


def test_http_error_body_does_not_raise_when_body_read_fails() -> None:
    class FakeHttpError:
        def read(self, _size: int) -> bytes:
            raise OSError("response stream closed")

        def __str__(self) -> str:
            return "HTTP 503 Service Unavailable"

    error: Any = FakeHttpError()

    assert metadata_helpers_module._http_error_body(error) == (
        "HTTP 503 Service Unavailable"
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("CON", "_CON"),
        ("con.txt", "_con.txt"),
        ("PRN.", "_PRN"),
        ("AUX ", "_AUX"),
        ("NUL.html", "_NUL.html"),
        ("COM1", "_COM1"),
        ("com9.pdf", "_com9.pdf"),
        ("LPT1", "_LPT1"),
        ("lpt9.txt", "_lpt9.txt"),
        ("ordinary paper", "ordinary paper"),
    ],
)
def test_metadata_safe_filename_avoids_windows_device_names(
    value: str,
    expected: str,
) -> None:
    assert metadata_helpers_module._safe_filename(value) == expected


def test_scihub_job_query_decoder_bounds_count_and_deduplicates() -> None:
    parts = [
        "doi:10.1000%2Fsame",
        "DOI:10.1000%2FSAME",
        *(f"doi:10.1000%2F{index}" for index in range(40)),
    ]

    queries = metadata_helpers_module._scihub_queries_from_job(
        {"queue_key": f"v=scihub-pdf-legacy|query_list={','.join(parts)}"}
    )

    assert len(queries) == metadata_helpers_module.MAX_SCIHUB_QUERY_CANDIDATES
    assert len(
        {
            (candidate["type"].casefold(), candidate["query"].casefold())
            for candidate in queries
        }
    ) == len(queries)
    assert queries[0] == {"type": "doi", "query": "10.1000/same"}


def test_scihub_job_query_decoder_bounds_legacy_components() -> None:
    oversized_query = "x" * (metadata_helpers_module.MAX_SCIHUB_QUERY_CHARS + 1)
    oversized_type = "t" * 33

    assert (
        metadata_helpers_module._scihub_query_from_job(
            {"queue_key": f"v=legacy|query={oversized_query}"}
        )
        == ""
    )
    assert (
        metadata_helpers_module._scihub_query_type_from_job(
            {"queue_key": f"v=legacy|query_type={oversized_type}|query=10.1%2Fok"}
        )
        == "doi"
    )
    assert metadata_helpers_module._scihub_queries_from_job(
        {"queue_key": (f"v=legacy|query_type={oversized_type}|query=10.1%2Fok")}
    ) == [{"type": "doi", "query": "10.1/ok"}]


@pytest.mark.parametrize(
    "queue_key",
    [
        True,
        7,
        ["v=legacy|query=10.1%2Flist"],
        {"value": "v=legacy|query=10.1%2Fmapping"},
    ],
)
def test_scihub_job_query_decoder_rejects_non_string_queue_key(
    queue_key: object,
) -> None:
    job = {"queue_key": queue_key}

    assert metadata_helpers_module._scihub_queries_from_job(job) == []
    assert metadata_helpers_module._scihub_query_from_job(job) == ""
    assert metadata_helpers_module._scihub_query_type_from_job(job) == "doi"


def test_scihub_drain_bounds_attempts_from_legacy_candidate_list(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    captured: dict[str, Any] = {}

    def fake_download_and_attach(_config: object, options: object) -> dict[str, Any]:
        calls.append(str(getattr(options, "doi")))
        return {"ok": False, "status": "unresolved", "error": "not found"}

    class FakeState:
        def mark_metadata_job_skipped(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"status": "skipped", **kwargs}

    monkeypatch.setattr(
        metadata_processor_module,
        "download_and_attach_scihub_pdf",
        fake_download_and_attach,
    )
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(
        ingest_data_root=tmp_path,
        scihub_mirrors=(),
        scihub_user_agent="test",
        scihub_request_timeout_seconds=1,
    )
    processor.state = FakeState()
    encoded = ",".join(f"doi:10.1000%2F{index}" for index in range(40))

    result = processor._drain_scihub_pdf_job(
        {
            "job_id": "job-bounded-legacy-queries",
            "lease_owner": "owner-a",
            "attempts": 1,
            "max_attempts": 3,
            "parent_item_key": "PARENT1",
            "queue_key": f"v=scihub-pdf-legacy|query_list={encoded}",
        }
    )

    assert result["status"] == "skipped"
    assert len(calls) == metadata_helpers_module.MAX_SCIHUB_QUERY_CANDIDATES
    assert len(captured["result"]["attempts"]) == len(calls)


def test_researchgate_url_rejects_oversized_queue_key_input() -> None:
    url = "https://www.researchgate.net/publication/" + ("x" * 5000)

    assert metadata_helpers_module._is_researchgate_url(url) is False


def test_binding_path_prefix_translation_requires_segment_boundary(
    tmp_path: Path,
) -> None:
    library_config = SimpleNamespace(
        zotero_path_prefix_map=(("C:/Zotero", tmp_path / "mapped"),)
    )

    candidates = backlog_scanner_module._path_candidates_from_binding(
        library_config,
        "C:/ZoteroOther/library",
    )

    assert candidates == [Path("C:/ZoteroOther/library")]


def test_source_html_cleanup_preserves_zero_as_an_explicit_noop_budget(
    monkeypatch: Any,
) -> None:
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor._library_configs = lambda **_kwargs: [SimpleNamespace(library_id="LIB1")]
    seen_limits: list[int] = []

    def fake_cleanup(**kwargs: Any) -> dict[str, Any]:
        seen_limits.append(kwargs["max_items"])
        return {
            "ok": True,
            "scanned": 0,
            "affected_parents": 0,
            "candidate_count": 0,
        }

    monkeypatch.setattr(
        metadata_processor_module,
        "cleanup_source_html_library",
        fake_cleanup,
    )
    monkeypatch.setattr(
        metadata_processor_module,
        "LocalZoteroStore",
        lambda _config: SimpleNamespace(),
    )

    result = processor.source_html_cleanup(
        max_items=0,
        limit=7,
        dry_run=True,
    )

    assert result["ok"] is True
    assert result["scanned"] == 0
    assert seen_limits == [0]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_items": -1},
        {"limit": -1},
        {"max_items": True},
        {"limit": "7"},
        {"max_items": 1_000_001},
        {"limit": 1_000_001},
    ],
)
def test_source_html_cleanup_rejects_invalid_direct_budget_before_scan(
    monkeypatch: Any,
    kwargs: dict[str, object],
) -> None:
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor._library_configs = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("Invalid cleanup budgets must fail before library discovery.")
    )
    monkeypatch.setattr(
        metadata_processor_module,
        "cleanup_source_html_library",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("Invalid cleanup budgets must not scan.")
        ),
    )

    with pytest.raises(ValueError, match="max_items|limit"):
        processor.source_html_cleanup(dry_run=True, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("limit", [0, -1, 50_001, True, "1"])
def test_core_drain_rejects_unsafe_limit_before_state_access(
    limit: object,
) -> None:
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(
        metadata_job_lease_seconds=60,
        metadata_drain_max_workers=1,
        zotero_relay_url="",
    )

    class UnexpectedState:
        def list_metadata_jobs(self, **_kwargs: Any) -> list[dict[str, Any]]:
            raise AssertionError("Invalid drain limit must not read queue rows.")

        def recover_expired_metadata_jobs(self, **_kwargs: Any) -> int:
            raise AssertionError("Invalid drain limit must not recover leases.")

        def metadata_queue_summary(self, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("Invalid drain limit must not inspect queue state.")

    processor.state = UnexpectedState()

    with pytest.raises(ValueError, match="limit"):
        processor._drain_queue(
            job_type=METADATA_JOB_FULL_TEXT,
            limit=limit,  # type: ignore[arg-type]
            dry_run=False,
            require_relay=False,
            policy=None,
        )


def test_core_drain_accepts_maximum_bounded_limit() -> None:
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(metadata_job_lease_seconds=60)
    seen_limits: list[int] = []

    class FakeState:
        def list_metadata_jobs(self, **kwargs: Any) -> list[dict[str, Any]]:
            seen_limits.append(kwargs["limit"])
            return []

        def metadata_queue_summary(self, **_kwargs: Any) -> dict[str, Any]:
            return {"queued": 0}

    processor.state = FakeState()
    result = processor._drain_queue(
        job_type=METADATA_JOB_FULL_TEXT,
        limit=50_000,
        dry_run=True,
        require_relay=False,
        policy=None,
    )
    assert result["would_process"] == 0
    assert seen_limits == [50_000]
