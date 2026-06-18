from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import zotero_ingest_worker.full_text_attachment as full_text_attachment
from zotero_ingest_worker.full_text_attachment import FullTextAttachmentService
from zotero_ingest_worker.full_text_article import html_download_article_verdict


def _article_html() -> dict[str, object]:
    return {
        "ok": True,
        "reason": "article_html",
        "text_chars": 25_000,
        "markers": ["article_tag", "article_body"],
        "section_markers": ["abstract", "methods", "results", "references"],
    }


def test_article_verdict_rejects_weak_publisher_landing() -> None:
    verdict = html_download_article_verdict(
        {
            "ok": True,
            "kind": "landing",
            "url": "https://www.tandfonline.com/doi/full/10.1000/example",
            "output_path": "/tmp/landing.html",
            "article": {
                "ok": True,
                "title": "Example Article - Get Access",
                "text_chars": 8_700,
                "markers": ["citation_title", "abstract", "references"],
                "section_markers": ["abstract", "references"],
            },
        }
    )

    assert verdict["ok"] is False
    assert verdict["reason"] == "access_landing"


def test_article_verdict_accepts_full_article_html() -> None:
    verdict = html_download_article_verdict(
        {
            "ok": True,
            "kind": "html",
            "url": "https://example.test/article",
            "output_path": "/tmp/article.html",
            "article": _article_html(),
        }
    )

    assert verdict["ok"] is True


def test_article_verdict_accepts_doi_redirect_to_full_article() -> None:
    verdict = html_download_article_verdict(
        {
            "ok": True,
            "kind": "landing",
            "url": "https://doi.org/10.1000/example",
            "final_url": "https://publisher.example/articles/10.1000/example",
            "output_path": "/tmp/article.html",
            "article": _article_html(),
        }
    )

    assert verdict["ok"] is True


def test_full_text_attachment_service_attaches_html_without_processor(tmp_path: Path) -> None:
    source = tmp_path / "article.html"
    source.write_text("<html><body>Article</body></html>", encoding="utf-8")
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    captured: dict[str, Any] = {}

    def create_parent_attachment(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"ok": True, "newAttachmentKey": "HTML1234"}

    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=create_parent_attachment,
        enqueue_pdf_for_ocr=lambda **_kwargs: {"unexpected": "ocr"},
        enqueue_pdf_for_html=lambda **_kwargs: {"unexpected": "html"},
    )

    result = service.attach(
        attachment=attachment,
        metadata=metadata,
        inventory={"attachments": []},
        payload={
            "html_downloads": [{"ok": True, "output_path": str(source), "article": _article_html()}],
            "pdf_downloads": [],
        },
    )

    assert result is not None
    assert result["kind"] == "html"
    assert captured["content_type"] == "text/html"
    assert captured["dedupe_prefix"] == "full-text-html"
    local_copy = Path(result["local_copy"]["path"])
    assert local_copy.suffix == ".html"
    assert local_copy.read_text(encoding="utf-8") == "<html><body>Article</body></html>"


def test_full_text_attachment_service_skips_html_when_source_html_exists(tmp_path: Path) -> None:
    source = tmp_path / "article.html"
    source.write_text("<html><body>New Article</body></html>", encoding="utf-8")
    existing = tmp_path / "storage" / "HTML0001" / "Article [SOURCE HTML].html"
    existing.parent.mkdir(parents=True)
    existing.write_text("<html><body>Existing Article</body></html>", encoding="utf-8")
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    create_calls: list[dict[str, Any]] = []

    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=lambda **kwargs: create_calls.append(kwargs) or {"ok": True},
        enqueue_pdf_for_ocr=lambda **_kwargs: {"unexpected": "ocr"},
        enqueue_pdf_for_html=lambda **_kwargs: {"unexpected": "html"},
    )

    result = service.attach(
        attachment=attachment,
        metadata=metadata,
        inventory={
            "has_html": True,
            "has_source_html": True,
            "attachments": [
                {
                    "key": "HTML0001",
                    "content_type": "text/html",
                    "path": "storage:Article [SOURCE HTML].html",
                    "title": "Article [source HTML]",
                    "file_path": str(existing),
                    "exists": True,
                }
            ],
        },
        payload={
            "html_downloads": [{"ok": True, "output_path": str(source), "article": _article_html()}],
            "pdf_downloads": [],
        },
    )

    assert result is not None
    assert result["skipped"] is True
    assert result["reason"] == "parent_already_has_source_html"
    assert result["existing_attachment_key"] == "HTML0001"
    assert create_calls == []


def test_full_text_attachment_service_trashes_dangling_source_html_before_attach(tmp_path: Path) -> None:
    source = tmp_path / "article.html"
    source.write_text("<html><body>Article</body></html>", encoding="utf-8")
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    trash_calls: list[dict[str, Any]] = []
    create_calls: list[dict[str, Any]] = []

    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=lambda **kwargs: create_calls.append(kwargs) or {"ok": True, "newAttachmentKey": "HTML1234"},
        enqueue_pdf_for_ocr=lambda **_kwargs: {"unexpected": "ocr"},
        enqueue_pdf_for_html=lambda **_kwargs: {"unexpected": "html"},
        trash_source_html_attachment=lambda **kwargs: trash_calls.append(kwargs) or {"ok": True, "trashed": True},
    )

    result = service.attach(
        attachment=attachment,
        metadata=metadata,
        inventory={
            "has_html": False,
            "has_source_html": False,
            "attachments": [
                {
                    "key": "HTMLDEAD",
                    "content_type": "text/html",
                    "path": "storage:Article [SOURCE HTML].html",
                    "title": "Article [source HTML]",
                    "file_path": str(tmp_path / "storage" / "HTMLDEAD" / "Article [SOURCE HTML].html"),
                    "exists": False,
                }
            ],
        },
        payload={
            "html_downloads": [{"ok": True, "output_path": str(source), "article": _article_html()}],
            "pdf_downloads": [],
        },
    )

    assert result is not None
    assert result["kind"] == "html"
    assert [call["attachment"].key for call in trash_calls] == ["HTMLDEAD"]
    assert trash_calls[0]["dry_run"] is False
    assert create_calls[0]["content_type"] == "text/html"
    assert result["source_html_cleanup"]["candidate_count"] == 1


def test_full_text_attachment_service_still_attaches_pdf_when_html_already_exists(tmp_path: Path) -> None:
    html = tmp_path / "article.html"
    html.write_text("<html><body>Article</body></html>", encoding="utf-8")
    pdf = tmp_path / "article.pdf"
    pdf.write_bytes(b"%PDF")
    existing = tmp_path / "storage" / "HTML0001" / "Article [SOURCE HTML].html"
    existing.parent.mkdir(parents=True)
    existing.write_text("<html><body>Existing Article</body></html>", encoding="utf-8")
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    create_calls: list[dict[str, Any]] = []

    def create_parent_attachment(**kwargs: Any) -> dict[str, Any]:
        create_calls.append(kwargs)
        return {"ok": True, "newAttachmentKey": "PDF1234"}

    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=create_parent_attachment,
        enqueue_pdf_for_ocr=lambda **_kwargs: {"unexpected": "ocr"},
        enqueue_pdf_for_html=lambda **_kwargs: {"classification": "skipped"},
    )

    result = service.attach(
        attachment=attachment,
        metadata=metadata,
        inventory={
            "has_html": True,
            "has_pdf": False,
            "has_source_html": True,
            "attachments": [
                {
                    "key": "HTML0001",
                    "content_type": "text/html",
                    "path": "storage:Article [SOURCE HTML].html",
                    "title": "Article [source HTML]",
                    "file_path": str(existing),
                    "exists": True,
                }
            ],
        },
        payload={
            "html_downloads": [{"ok": True, "output_path": str(html), "article": _article_html()}],
            "pdf_downloads": [
                {
                    "ok": True,
                    "status": "downloaded",
                    "output_path": str(pdf),
                    "identity": {"needs_ocr": False},
                }
            ],
        },
    )

    assert result is not None
    assert result["kind"] == "pdf"
    assert result["html_attachment"]["skipped"] is True
    assert [call["content_type"] for call in create_calls] == ["application/pdf"]


def test_full_text_attachment_service_reports_local_metadata_failure(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    source.write_text("<html><body>Article</body></html>", encoding="utf-8")
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")

    def fail_local_metadata(**_kwargs: Any) -> dict[str, Any]:
        raise OSError("database is locked")

    monkeypatch.setattr(full_text_attachment, "sync_parent_attachment_local", fail_local_metadata)

    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=lambda **_kwargs: {"ok": True, "newAttachmentKey": "HTML1234"},
        enqueue_pdf_for_ocr=lambda **_kwargs: {"unexpected": "ocr"},
        enqueue_pdf_for_html=lambda **_kwargs: {"unexpected": "html"},
    )

    result = service.attach(
        attachment=attachment,
        metadata=metadata,
        inventory={"attachments": []},
        payload={
            "html_downloads": [{"ok": True, "output_path": str(source), "article": _article_html()}],
            "pdf_downloads": [],
        },
    )

    assert result is not None
    assert result["ok"] is True
    assert result["local_copy"]["ok"] is True
    assert result["local_metadata"]["ok"] is False
    assert result["local_metadata"]["reason"] == "local_metadata_failed"
    assert "database is locked" in result["local_metadata"]["error"]


def test_full_text_attachment_service_enqueues_attached_html_for_translation(tmp_path: Path) -> None:
    source = tmp_path / "article.html"
    source.write_text("<html><body>Article</body></html>", encoding="utf-8")
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    translation_calls: list[dict[str, Any]] = []

    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=lambda **_kwargs: {"ok": True, "newAttachmentKey": "HTML1234"},
        enqueue_pdf_for_ocr=lambda **_kwargs: {"unexpected": "ocr"},
        enqueue_pdf_for_html=lambda **_kwargs: {"unexpected": "html"},
        enqueue_html_for_translation=lambda **kwargs: translation_calls.append(kwargs) or {"queued": 1},
    )

    result = service.attach(
        attachment=attachment,
        metadata=metadata,
        inventory={"attachments": []},
        payload={
            "html_downloads": [{"ok": True, "output_path": str(source), "article": _article_html()}],
            "pdf_downloads": [],
        },
    )

    assert result is not None
    assert result["translation_enqueue"] == {"queued": 1}
    assert translation_calls[0]["source_path"].name == "Article [SOURCE HTML].html"


def test_full_text_attachment_service_pdf_needing_ocr_uses_callback(tmp_path: Path) -> None:
    source = tmp_path / "article.pdf"
    source.write_bytes(b"%PDF")
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    ocr_calls: list[dict[str, Any]] = []

    def enqueue_pdf_for_ocr(**kwargs: Any) -> dict[str, Any]:
        ocr_calls.append(kwargs)
        return {"classification": "queued"}

    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=lambda **_kwargs: {"ok": True, "newAttachmentKey": "PDF1234"},
        enqueue_pdf_for_ocr=enqueue_pdf_for_ocr,
        enqueue_pdf_for_html=lambda **_kwargs: {"unexpected": "html"},
    )

    result = service.attach(
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
    assert result["ocr_enqueue"] == {"classification": "queued"}
    assert ocr_calls[0]["source_path"].name == "Article [FULL TEXT].pdf"


def test_full_text_attachment_service_attaches_pdf_when_parent_only_has_html(tmp_path: Path) -> None:
    source = tmp_path / "article.pdf"
    source.write_bytes(b"%PDF")
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    create_calls: list[dict[str, Any]] = []

    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=lambda **kwargs: create_calls.append(kwargs) or {"ok": True, "newAttachmentKey": "PDF1234"},
        enqueue_pdf_for_ocr=lambda **_kwargs: {"unexpected": "ocr"},
        enqueue_pdf_for_html=lambda **_kwargs: {"classification": "skipped"},
    )

    result = service.attach(
        attachment=attachment,
        metadata=metadata,
        inventory={"has_html": True, "has_pdf": False, "attachments": []},
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
    assert create_calls[0]["content_type"] == "application/pdf"
    assert create_calls[0]["dedupe_prefix"] == "full-text-pdf"
    assert Path(result["local_copy"]["path"]).read_bytes() == b"%PDF"


def test_full_text_attachment_service_attaches_html_and_pdf_when_both_found(tmp_path: Path) -> None:
    html = tmp_path / "article.html"
    html.write_text("<html><body>Article</body></html>", encoding="utf-8")
    pdf = tmp_path / "article.pdf"
    pdf.write_bytes(b"%PDF")
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    create_calls: list[dict[str, Any]] = []

    def create_parent_attachment(**kwargs: Any) -> dict[str, Any]:
        create_calls.append(kwargs)
        key = "HTML1234" if kwargs["content_type"] == "text/html" else "PDF1234"
        return {"ok": True, "newAttachmentKey": key}

    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=create_parent_attachment,
        enqueue_pdf_for_ocr=lambda **_kwargs: {"unexpected": "ocr"},
        enqueue_pdf_for_html=lambda **_kwargs: {"classification": "skipped"},
    )

    result = service.attach(
        attachment=attachment,
        metadata=metadata,
        inventory={"has_html": False, "has_pdf": False, "attachments": []},
        payload={
            "html_downloads": [{"ok": True, "output_path": str(html), "article": _article_html()}],
            "pdf_downloads": [
                {
                    "ok": True,
                    "status": "downloaded",
                    "output_path": str(pdf),
                    "identity": {"needs_ocr": False},
                }
            ],
        },
    )

    assert result is not None
    assert result["kind"] == "html"
    assert result["attached_kinds"] == ["html", "pdf"]
    assert result["pdf_attachment"]["kind"] == "pdf"
    assert [call["content_type"] for call in create_calls] == ["text/html", "application/pdf"]
    assert Path(result["local_copy"]["path"]).read_text(encoding="utf-8") == "<html><body>Article</body></html>"
    assert Path(result["pdf_local_copy"]["path"]).read_bytes() == b"%PDF"


def test_full_text_attachment_service_skips_pdf_when_parent_has_pdf(tmp_path: Path) -> None:
    source = tmp_path / "article.pdf"
    source.write_bytes(b"%PDF")
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    create_calls: list[dict[str, Any]] = []

    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=lambda **kwargs: create_calls.append(kwargs) or {"ok": True},
        enqueue_pdf_for_ocr=lambda **_kwargs: {"unexpected": "ocr"},
        enqueue_pdf_for_html=lambda **_kwargs: {"unexpected": "html"},
    )

    result = service.attach(
        attachment=attachment,
        metadata=metadata,
        inventory={"has_html": True, "has_pdf": True, "attachments": []},
        payload={
            "html_downloads": [],
            "pdf_downloads": [{"ok": True, "status": "downloaded", "output_path": str(source)}],
        },
    )

    assert result == {
        "ok": True,
        "skipped": True,
        "kind": "pdf",
        "reason": "parent_already_has_pdf",
        "has_html": True,
        "has_pdf": True,
        "source": {"ok": True, "status": "downloaded", "output_path": str(source)},
    }
    assert create_calls == []
