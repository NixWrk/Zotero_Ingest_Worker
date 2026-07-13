from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from zotero_ingest_worker.local_zotero import LocalAttachment, LocalZoteroStore
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


def test_local_zotero_iter_pdf_attachments_accepts_unbounded_limit(tmp_path: Path) -> None:
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

    assert empty_only == {"DOI": "10.1000/example", "publicationTitle": "Journal of Examples"}
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

    assert diff["patch"] == {"DOI": "10.1000/example", "publicationTitle": "Journal of Examples"}
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
    assert preprint["skipped_fields"]["ISSN"] == "field_not_valid_for_item_type:preprint"
    assert preprint["skipped_fields"]["publicationTitle"] == "field_not_valid_for_item_type:preprint"
    assert preprint["skipped_fields"]["volume"] == "field_not_valid_for_item_type:preprint"
    assert journal["patch"] == {
        "DOI": "10.48550/arXiv.2401.01234",
        "ISSN": "1234-5678",
        "publicationTitle": "Journal of Preprints",
        "volume": "12",
    }
    assert journal["skipped_fields"]["ISBN"] == "field_not_valid_for_item_type:journalArticle"
    assert journal["skipped_fields"]["websiteTitle"] == "field_not_valid_for_item_type:journalArticle"


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
    assert title_match_score("A careful metadata pipeline", "A Careful Metadata Pipeline") == 1.0
    assert title_match_score("Completely different", "A Careful Metadata Pipeline") < 0.5


def test_metadata_lookup_uses_shared_enricher(monkeypatch) -> None:
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace()
    processor._provider_events = []
    metadata = SimpleNamespace(fields={"title": "Example"}, title="Example", tags=[], relations=[])
    attachment = SimpleNamespace(
        filename="paper.pdf",
        zotero_path="storage:paper.pdf",
        file_path=Path("paper.pdf"),
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

        def lookup_candidate(self, **_kwargs: object) -> MetadataCandidate:
            return expected

    monkeypatch.setattr(processor, "_metadata_enricher", lambda: FakeEnricher())

    candidate = processor._lookup_metadata_candidate(metadata=metadata, attachment=attachment)

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

        def lease_next_metadata_job(self, *, owner: str, **_kwargs: object) -> dict[str, object] | None:
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

    monkeypatch.setattr(ZoteroMetadataProcessor, "_drain_leased_job", fake_drain_leased_job)

    result = processor.drain_full_text_queue(limit=5)

    assert result["processed"] == 5
    assert result["failed"] == 0
    assert result["workers"] == 3
    assert len({owner.rsplit("-", 1)[-1] for owner in fake_state.owners}) >= 2


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
    assert not _is_nonretryable_worker_error(RuntimeError("temporary connection refused"))


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
        exc=FileNotFoundError("Local Zotero PDF attachment is deleted or inactive: PDF1234"),
    )

    assert result["status"] == "skipped"
    assert captured["result"]["reason"] == "local_attachment_deleted_or_inactive"


def test_cloudflare_metadata_403_is_nonactionable() -> None:
    assert (
        _nonactionable_metadata_http_error_reason(
            403,
            '<html><head><title>Just a moment...</title></head><body>Cloudflare</body></html>',
        )
        == "metadata_provider_blocked"
    )
    assert _nonactionable_metadata_http_error_reason(500, "Cloudflare") is None


def test_metadata_patch_relay_payload_includes_library_id(monkeypatch: Any) -> None:
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(zotero_relay_url="http://relay")
    captured: dict[str, object] = {}

    def fake_request_json(self: ZoteroRelayClient, **kwargs: object) -> dict[str, object]:
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
            "parentCreated": {
                "key": "ITEM1234",
                "title": "paper",
                "itemType": "document",
                "version": 9,
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
    assert validate_arxiv_html("not html", min_text_chars=1)["reason"] == "missing_html_tag"
    assert validate_arxiv_html("<html><body>tiny</body></html>", min_text_chars=100)["reason"] == "too_little_text"


def test_arxiv_validation_failure_is_skipped_not_failed(monkeypatch: Any, tmp_path: Path) -> None:
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

    leased = store.lease_next_metadata_job(job_type="enrich", owner="test", lease_seconds=60)
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
    leased = store.lease_next_metadata_job(job_type="arxiv_html", owner="test", lease_seconds=60)
    assert leased is not None
    failed = store.mark_metadata_job_failed(
        job_id=str(leased["job_id"]),
        message="timed out",
        retryable=True,
    )
    assert failed["status"] == "failed_final"
    retried = store.retry_metadata_job(str(keep_attempts["job_id"]))
    assert retried["status"] == "queued"
    assert retried["attempts"] == 1
    store.cancel_metadata_job(str(keep_attempts["job_id"]))

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
    leased = store.lease_next_metadata_job(job_type="arxiv_html", owner="test", lease_seconds=60)
    assert leased is not None
    failed = store.mark_metadata_job_failed(
        job_id=str(leased["job_id"]),
        message="timed out",
        retryable=True,
    )
    assert failed["status"] == "failed_final"
    retried = store.retry_metadata_job(str(reset_attempts["job_id"]), reset_attempts=True)
    assert retried["status"] == "queued"
    assert retried["attempts"] == 0


def test_metadata_state_parent_scope_dedupes_changed_container_signature(tmp_path: Path) -> None:
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


def test_metadata_backlog_scan_dedupes_multiple_pdfs_for_same_parent(tmp_path: Path) -> None:
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


def test_full_text_backlog_scan_queues_parent_items_without_html(tmp_path: Path) -> None:
    data_dir = _write_full_text_fixture(tmp_path, with_pdf=False)
    config = _metadata_processor_test_config(tmp_path, data_dir)
    processor = ZoteroMetadataProcessor(config)
    processor._library_configs = lambda **_kwargs: [config]  # type: ignore[method-assign]

    result = processor.full_text_backlog_scan(limit=1)

    assert result["queued"] == 1
    assert result["results"][0]["parent_item_key"] == "PARENT1"
    assert result["results"][0]["classification"] == "queued"


def test_full_text_backlog_scan_queues_parent_items_with_html_but_without_pdf(tmp_path: Path) -> None:
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


def test_full_text_backlog_scan_queues_parent_items_with_pdf_but_without_source_html(tmp_path: Path) -> None:
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


def test_full_text_backlog_scan_skips_parent_items_with_html_and_pdf(tmp_path: Path) -> None:
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
    _add_second_parent(data_dir, key="PARENT2", title="Filtered Parent", doi="10.2000/filtered")
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

    def fake_download_and_attach_scihub_pdf(config: Any, options: Any) -> dict[str, Any]:
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
    _add_second_parent(data_dir, key="PARENT2", title="Filtered Parent", doi="10.2000/filtered")
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

    def fake_download_and_attach_scihub_pdf(config: Any, options: Any) -> dict[str, Any]:
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

    def fake_download_and_attach_scihub_pdf(config: Any, options: Any) -> dict[str, Any]:
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
    queued = processor.state.list_metadata_jobs(job_type="scihub_pdf", statuses={"queued"}, limit=1)[0]
    with processor.state._connect() as connection:
        connection.execute(
            "update metadata_jobs set max_attempts = 1 where job_id = ?",
            (queued["job_id"],),
        )

    def fake_download_and_attach_scihub_pdf(config: Any, options: Any) -> dict[str, Any]:
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


def test_full_text_backlog_scan_dedupes_parent_when_sqlite_mtime_changes(tmp_path: Path) -> None:
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

    result = processor.drain_full_text_queue(limit=1)

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

    result = processor.drain_full_text_queue(limit=1)
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

    assert full_text_worker_status(payload) == "existing_pdf_html_source_language_skipped"


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


def test_parent_attachment_relay_payload_includes_parent_item_key(monkeypatch: Any, tmp_path: Path) -> None:
    source = tmp_path / "article.html"
    source.write_text("<html></html>", encoding="utf-8")
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(zotero_relay_url="http://relay")
    captured: dict[str, object] = {}

    def fake_request_json(self: ZoteroRelayClient, **kwargs: object) -> dict[str, object]:
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
    )

    assert result == {"ok": True, "newAttachmentKey": "HTML1234"}
    assert captured["path"] == "/attachments/parents/ITEM1234/attachments/file"
    payload = captured["payload"]
    assert payload["libraryId"] == "LIB1"  # type: ignore[index]
    assert payload["contentType"] == "text/html"  # type: ignore[index]
    assert payload["probeAttachmentKey"] == "PDF1234"  # type: ignore[index]


def test_html_attachment_source_embeds_local_assets(tmp_path: Path) -> None:
    source = tmp_path / "01.source.html"
    assets_dir = tmp_path / "01.source_assets"
    assets_dir.mkdir()
    (assets_dir / "fig.png").write_bytes(b"PNG")
    (assets_dir / "style.css").write_text("body { background: url(fig.png); }", encoding="utf-8")
    source.write_text(
        (
            "<html><head><link rel=\"stylesheet\" href=\"01.source_assets/style.css\"></head>"
            "<body><img src=\"01.source_assets/fig.png\"></body></html>"
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


def test_html_attachment_source_inlines_style_import_assets(tmp_path: Path) -> None:
    source = tmp_path / "01.source.html"
    assets_dir = tmp_path / "01.source_assets"
    assets_dir.mkdir()
    (assets_dir / "fig.png").write_bytes(b"PNG")
    (assets_dir / "base.css").write_text("body { background: url(fig.png); }", encoding="utf-8")
    (assets_dir / "style.css").write_text(
        "@import \"base.css\" layer(base); .article { color: red; }",
        encoding="utf-8",
    )
    source.write_text(
        (
            "<html><head><style>@import \"01.source_assets/style.css\" layer(article);</style></head>"
            "<body><img src=\"01.source_assets/fig.png\"></body></html>"
        ),
        encoding="utf-8",
    )

    embedded_path, report = _html_attachment_source_with_embedded_assets(source)

    assert embedded_path != source
    assert embedded_path.name == "01.source.z2m_embedded.html"
    assert report["enabled"] is True
    assert report["embedded_stylesheets"] == 2
    saved = embedded_path.read_text(encoding="utf-8")
    assert "@import" not in saved
    assert "@layer article" in saved
    assert "@layer base" in saved
    assert ".article { color: red; }" in saved
    assert "data:image/png;base64," in saved


def test_full_text_attach_html_uses_embedded_asset_file(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "01.source.html"
    assets_dir = tmp_path / "01.source_assets"
    assets_dir.mkdir()
    (assets_dir / "fig.png").write_bytes(b"PNG")
    source.write_text("<html><body><img src=\"01.source_assets/fig.png\"></body></html>", encoding="utf-8")
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
        return {"ok": True, "newAttachmentKey": "HTML9999"}

    monkeypatch.setattr(processor, "_create_parent_attachment_via_relay", fake_create_parent_attachment_via_relay)

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
    assert relay_source.name == "article.z2m_embedded.html"
    assert result["raw_source_path"] == str(source)
    assert result["article_standard"]["ok"] is True
    assert captured["content_type"] == "text/html"
    assert result["embedded_assets"]["enabled"] is True
    local_copy = Path(result["local_copy"]["path"])
    assert local_copy.suffix == ".html"
    saved = local_copy.read_text(encoding="utf-8")
    assert "_assets/" not in saved
    assert "data:image/png;base64," in saved


def test_html_attachment_source_without_assets_uses_original_file(tmp_path: Path) -> None:
    source = tmp_path / "article.html"
    source.write_text("<html><body>Article</body></html>", encoding="utf-8")

    attachment_source, report = _html_attachment_source_with_embedded_assets(source)

    assert attachment_source == source
    assert report == {"enabled": False, "reason": "assets_dir_missing"}


def test_html_attachment_source_with_empty_assets_uses_original_file(tmp_path: Path) -> None:
    source = tmp_path / "article.html"
    source.write_text("<html><body>Article</body></html>", encoding="utf-8")
    (tmp_path / "article_assets").mkdir()

    attachment_source, report = _html_attachment_source_with_embedded_assets(source)

    assert attachment_source == source
    assert report["enabled"] is False
    assert report["reason"] == "assets_empty"


def test_html_attachment_source_reports_missing_css_asset_and_keeps_external_url(tmp_path: Path) -> None:
    source = tmp_path / "article.html"
    assets_dir = tmp_path / "article_assets"
    assets_dir.mkdir()
    (assets_dir / "style.css").write_text(
        "body { background: url(missing.png); } .logo { background: url(https://cdn.example/logo.png); }",
        encoding="utf-8",
    )
    source.write_text(
        "<html><head><link rel=\"stylesheet\" href=\"article_assets/style.css\"></head><body></body></html>",
        encoding="utf-8",
    )

    attachment_source, report = _html_attachment_source_with_embedded_assets(source)

    assert attachment_source != source
    assert report["enabled"] is True
    assert report["embedded_stylesheets"] == 1
    assert report["missing_local_refs"] == ["missing.png"]
    saved = attachment_source.read_text(encoding="utf-8")
    assert "<link" not in saved
    assert "https://cdn.example/logo.png" in saved
    assert "missing.png" in saved


def test_full_text_attach_returns_none_without_relay(tmp_path: Path) -> None:
    source = tmp_path / "article.html"
    source.write_text("<html></html>", encoding="utf-8")
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(zotero_relay_url="")

    result = processor._attach_full_text_result(
        attachment=SimpleNamespace(storage_dir=tmp_path / "storage"),
        metadata=SimpleNamespace(title="Article"),
        inventory={"attachments": []},
        payload={"html_downloads": [{"ok": True, "output_path": str(source)}], "pdf_downloads": []},
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
                {"ok": True, "output_path": str(missing), "article": _valid_full_article_assessment()}
            ],
            "pdf_downloads": [],
        },
    )

    assert result == {"ok": False, "status": "local_source_missing", "sourcePath": str(missing)}


def test_full_text_attach_html_without_assets_sends_original_file(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    source.write_text("<html><body>Article</body></html>", encoding="utf-8")
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(zotero_relay_url="http://relay")
    metadata = SimpleNamespace(library_id="LIB1", data_dir=tmp_path, key="ITEM1234", item_id=10, title="Article")
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    captured: dict[str, object] = {}

    def fake_create_parent_attachment_via_relay(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"ok": True, "newAttachmentKey": "HTML0001"}

    monkeypatch.setattr(processor, "_create_parent_attachment_via_relay", fake_create_parent_attachment_via_relay)

    result = processor._attach_full_text_result(
        attachment=attachment,
        metadata=metadata,
        inventory={"attachments": []},
        payload={"html_downloads": [{"ok": True, "output_path": str(source), "article": _valid_full_article_assessment()}], "pdf_downloads": []},
    )

    assert result is not None
    assert result["kind"] == "html"
    relay_source = Path(captured["source_path"])  # type: ignore[arg-type]
    assert relay_source.name == "article.html"
    assert "article_packages" in relay_source.parts
    assert result["raw_source_path"] == str(source)
    assert captured["content_type"] == "text/html"
    assert result["embedded_assets"] == {"enabled": False, "reason": "assets_dir_missing"}
    assert Path(result["local_copy"]["path"]).read_text(encoding="utf-8") == "<html><body>Article</body></html>"


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
    metadata = SimpleNamespace(library_id="LIB1", data_dir=tmp_path, key="ITEM1234", item_id=10, title="Article")
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    calls: list[dict[str, object]] = []

    def fake_create_parent_attachment_via_relay(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        key = "HTML0002" if kwargs["content_type"] == "text/html" else "PDF0002"
        return {"ok": True, "newAttachmentKey": key}

    monkeypatch.setattr(processor, "_create_parent_attachment_via_relay", fake_create_parent_attachment_via_relay)

    result = processor._attach_full_text_result(
        attachment=attachment,
        metadata=metadata,
        inventory={"attachments": []},
        payload={
            "html_downloads": [{"ok": True, "output_path": str(html), "article": _valid_full_article_assessment()}],
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
    assert relay_html.name == "article.html"
    assert "article_packages" in relay_html.parts
    assert result["raw_source_path"] == str(html)
    assert calls[1]["source_path"] == pdf


def test_full_text_attach_returns_none_when_no_download_succeeded(tmp_path: Path) -> None:
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace(zotero_relay_url="http://relay")

    result = processor._attach_full_text_result(
        attachment=SimpleNamespace(storage_dir=tmp_path / "storage"),
        metadata=SimpleNamespace(title="Article"),
        inventory={"attachments": []},
        payload={
            "html_downloads": [{"ok": False, "status": "title_mismatch", "output_path": ""}],
            "pdf_downloads": [{"ok": False, "status": "identity_mismatch", "output_path": ""}],
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
        lambda **_kwargs: {"ok": True, "newAttachmentKey": "PDF9999"},
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
        lambda **_kwargs: {"ok": True, "newAttachmentKey": "PDF8888"},
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
            "section_markers": ["abstract", "methods", "results", "discussion", "references"],
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
    (data_dir / "storage" / key / filename).write_text("<html><body>Article</body></html>", encoding="utf-8")


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
