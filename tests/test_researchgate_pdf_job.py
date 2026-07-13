from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import zotero_ingest_worker.metadata_processor as metadata_processor
import zotero_ingest_worker.researchgate_pdf as researchgate_pdf
from zotero_ingest_worker.metadata_processor import ZoteroMetadataProcessor
from zotero_ingest_worker.local_zotero import LocalItemMetadata


def test_researchgate_pdf_adapter_uses_passed_config(monkeypatch: Any, tmp_path: Path) -> None:
    calls: dict[str, Any] = {}
    config = SimpleNamespace(name="config")

    async def fake_download(**kwargs: Any) -> dict[str, Any]:
        calls["download"] = kwargs
        output_path = tmp_path / "paper.pdf"
        output_path.write_bytes(b"%PDF")
        return {"ok": True, "status": "downloaded", "output_path": str(output_path)}

    def fake_attach(passed_config: object, **kwargs: Any) -> dict[str, Any]:
        calls["attach_config"] = passed_config
        calls["attach"] = kwargs
        return {"ok": True, "status": "attached", "relay": {"newAttachmentKey": "PDF1234"}}

    monkeypatch.setattr(
        researchgate_pdf,
        "_script_module",
        lambda: SimpleNamespace(
            preflight_pdf_attach=lambda *_args, **_kwargs: {"ok": True, "skipped": False},
            download_researchgate_pdf=fake_download,
            attach_pdf_to_zotero_parent=fake_attach,
            DEFAULT_DOWNLOAD_DIR=tmp_path,
            DEFAULT_PROFILE_DIR=tmp_path / "profile",
        ),
    )

    result = asyncio.run(
        researchgate_pdf.download_and_attach_researchgate_pdf(
            config,  # type: ignore[arg-type]
            researchgate_pdf.ResearchGatePdfOptions(
                url="https://www.researchgate.net/publication/example",
                item_key="ITEM1",
                data_dir="Zotero",
            ),
        )
    )

    assert result["ok"] is True
    assert calls["download"]["url"] == "https://www.researchgate.net/publication/example"
    assert calls["attach_config"] is config
    assert calls["attach"]["item_key"] == "ITEM1"


def test_researchgate_pdf_drain_job_marks_success(monkeypatch: Any, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    async def fake_download_and_attach(_config: object, options: object) -> dict[str, Any]:
        captured["options"] = options
        output_path = tmp_path / "paper.pdf"
        output_path.write_bytes(b"%PDF")
        return {
            "ok": True,
            "status": "attached",
            "download": {"ok": True, "output_path": str(output_path)},
            "attach": {"ok": True, "relay": {"newAttachmentKey": "PDF1234"}},
        }

    class FakeState:
        def mark_metadata_job_succeeded(self, **kwargs: Any) -> dict[str, Any]:
            captured["succeeded"] = kwargs
            return {"status": "succeeded", **kwargs}

        def mark_metadata_job_skipped(self, **kwargs: Any) -> dict[str, Any]:
            captured["skipped"] = kwargs
            return {"status": "skipped", **kwargs}

        def mark_metadata_job_failed(self, **kwargs: Any) -> dict[str, Any]:
            captured["failed"] = kwargs
            return {"status": "failed_final", **kwargs}

    monkeypatch.setattr(metadata_processor, "download_and_attach_researchgate_pdf", fake_download_and_attach)
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace()
    processor.state = FakeState()
    url = "https://www.researchgate.net/publication/example"

    result = processor._drain_researchgate_pdf_job(
        {
            "job_id": "job_1",
            "lease_owner": "owner-a",
            "parent_item_key": "ITEM1",
            "attachment_key": "ITEM1",
            "data_dir": str(tmp_path),
            "queue_key": "v=researchgate-pdf-browser-1|url=https%3A%2F%2Fwww.researchgate.net%2Fpublication%2Fexample",
        }
    )

    assert result["status"] == "succeeded"
    assert captured["options"].url == url
    assert captured["options"].item_key == "ITEM1"
    assert captured["succeeded"]["output_path"] == str(tmp_path / "paper.pdf")
    assert captured["succeeded"]["owner"] == "owner-a"


def test_researchgate_fallback_ignores_non_researchgate_urls() -> None:
    payload = {
        "browser_fallbacks": [
            {"url": "https://example.test/browser-only"},
            {"url": "https://www.researchgate.net/publication/example"},
        ]
    }

    fallback = metadata_processor._first_researchgate_browser_fallback(payload)

    assert fallback == {"url": "https://www.researchgate.net/publication/example"}


def test_researchgate_browser_fallback_enqueues_without_undefined_force(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class FakeState:
        def enqueue_metadata_job(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"job_id": "researchgate-1", "created": True, **kwargs}

    sqlite_path = tmp_path / "zotero.sqlite"
    sqlite_path.write_bytes(b"state")
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace()
    processor.state = FakeState()
    processor._queue_key = lambda _job_type: "researchgate-v1"
    metadata = LocalItemMetadata(
        library_id="LIB1",
        data_dir=tmp_path,
        key="PARENT1",
        item_id=1,
        version=None,
        item_type="journalArticle",
        date_modified=None,
        fields={"title": "Paper"},
        creators=[],
        tags=[],
        collections=[],
        relations=[],
    )

    result = processor._enqueue_researchgate_pdf_fallback(
        metadata=metadata,
        payload={
            "browser_fallbacks": [
                {"url": "https://www.researchgate.net/publication/example"}
            ]
        },
        reason="test",
    )

    assert result is not None
    assert result["classification"] == "queued"
    assert captured["force"] is False
    assert captured["parent_item_key"] == "PARENT1"
