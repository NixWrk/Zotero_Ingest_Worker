from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

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


def _native_article_html() -> str:
    body = " ".join(
        [
            "This complete article paragraph contains methods, results, evidence, and discussion."
        ]
        * 80
    )
    return (
        "<html><head><title>Article</title></head><body>"
        f"<article><h1>Article</h1><p>{body}</p></article>"
        "</body></html>"
    )


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


def test_full_text_attachment_service_attaches_html_without_processor(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    source.write_text(_native_article_html(), encoding="utf-8")
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
            "html_downloads": [
                {"ok": True, "output_path": str(source), "article": _article_html()}
            ],
            "pdf_downloads": [],
        },
    )

    assert result is not None
    assert result["kind"] == "html"
    assert captured["content_type"] == "text/html"
    assert captured["dedupe_prefix"] == "full-text-html"
    assert len(str(captured["source_sha256"])) == 64
    assert (
        str(captured["source_sha256"]) == result["article_standard"]["article_sha256"]
    )
    local_copy = Path(result["local_copy"]["path"])
    assert local_copy.suffix == ".html"
    attached_html = local_copy.read_text(encoding="utf-8")
    assert 'id="web-doc"' in attached_html
    assert "This complete article paragraph" in attached_html
    assert result["article_standard"]["ok"] is True
    assert result["raw_html_fallback"] is False


@pytest.mark.parametrize("output", [None, "invalid"])
def test_full_text_attachment_service_requires_enabled_embedding_output_record(
    monkeypatch: Any,
    tmp_path: Path,
    output: object,
) -> None:
    source = tmp_path / "article.html"
    source.write_text(_native_article_html(), encoding="utf-8")
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    create_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        full_text_attachment,
        "standardize_native_html_download",
        lambda *_args, **_kwargs: {"ok": False, "reason": "use_raw_test_source"},
    )

    def incomplete_embedding_report(
        source_path: Path,
        **_kwargs: object,
    ) -> tuple[Path, dict[str, object]]:
        report: dict[str, object] = {"enabled": True}
        if output is not None:
            report["output"] = output
        return source_path, report

    monkeypatch.setattr(
        full_text_attachment,
        "_html_attachment_source_with_embedded_assets",
        incomplete_embedding_report,
    )
    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=lambda **kwargs: (
            create_calls.append(kwargs) or {"ok": True, "newAttachmentKey": "HTML1234"}
        ),
        enqueue_pdf_for_ocr=lambda **_kwargs: {"unexpected": "ocr"},
        enqueue_pdf_for_html=lambda **_kwargs: {"unexpected": "html"},
        allow_raw_html_fallback=True,
    )

    result = service.attach(
        attachment=attachment,
        metadata=metadata,
        inventory={"attachments": []},
        payload={
            "html_downloads": [
                {
                    "ok": True,
                    "output_path": str(source),
                    "article": _article_html(),
                }
            ],
            "pdf_downloads": [],
        },
    )

    assert result is not None
    assert result["ok"] is False
    assert result["status"] == "embedded_output_integrity_mismatch"
    assert create_calls == []


def test_parent_attachment_local_copy_rejects_source_mutation(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.html"
    source.write_bytes(b"A" * 64)
    expected = full_text_attachment._stable_file_fingerprint(
        source,
        max_bytes=128,
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    original_copy = full_text_attachment._copy_file_bounded

    def copy_then_mutate(
        source_path: Path,
        target_path: Path,
        *,
        max_bytes: int | None,
    ) -> Any:
        result = original_copy(source_path, target_path, max_bytes=max_bytes)
        if source_path == source and result is not None:
            source.write_bytes(b"B" * 64)
        return result

    monkeypatch.setattr(
        full_text_attachment,
        "_copy_file_bounded",
        copy_then_mutate,
    )

    with pytest.raises(OSError, match="changed during local copy"):
        full_text_attachment.write_parent_attachment_local_copy(
            attachment=attachment,
            source_path=source,
            filename="Article.html",
            relay_result={"newAttachmentKey": "HTML1234"},
            expected_source=expected,
        )

    target_dir = tmp_path / "storage" / "HTML1234"
    assert not (target_dir / "Article.html").exists()
    assert not list(target_dir.glob("*.full-text-tmp-*"))


def test_full_text_attachment_service_rejects_html_when_polish_fails(
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
    create_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        full_text_attachment,
        "standardize_native_html_download",
        lambda *_args, **_kwargs: {"ok": False, "reason": "quality_failed"},
    )

    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=lambda **kwargs: (
            create_calls.append(kwargs) or {"ok": True}
        ),
        enqueue_pdf_for_ocr=lambda **_kwargs: {"unexpected": "ocr"},
        enqueue_pdf_for_html=lambda **_kwargs: {"unexpected": "html"},
    )

    result = service.attach(
        attachment=attachment,
        metadata=metadata,
        inventory={"attachments": []},
        payload={
            "html_downloads": [
                {"ok": True, "output_path": str(source), "article": _article_html()}
            ],
            "pdf_downloads": [],
        },
    )

    assert result is not None
    assert result["ok"] is False
    assert result["status"] == "source_html_polish_failed"
    assert result["article_standard"] == {"ok": False, "reason": "quality_failed"}
    assert create_calls == []


def test_full_text_attachment_service_raw_html_fallback_is_explicit(
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
    create_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        full_text_attachment,
        "standardize_native_html_download",
        lambda *_args, **_kwargs: {"ok": False, "reason": "quality_failed"},
    )

    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=lambda **kwargs: (
            create_calls.append(kwargs)
            or {
                "ok": True,
                "newAttachmentKey": "HTML1234",
            }
        ),
        enqueue_pdf_for_ocr=lambda **_kwargs: {"unexpected": "ocr"},
        enqueue_pdf_for_html=lambda **_kwargs: {"unexpected": "html"},
        allow_raw_html_fallback=True,
    )

    result = service.attach(
        attachment=attachment,
        metadata=metadata,
        inventory={"attachments": []},
        payload={
            "html_downloads": [
                {"ok": True, "output_path": str(source), "article": _article_html()}
            ],
            "pdf_downloads": [],
        },
    )

    assert result is not None
    assert result["ok"] is True
    assert result["raw_html_fallback"] is True
    assert Path(result["attachment_source_path"]) == source
    relay_source = Path(create_calls[0]["source_path"])
    assert relay_source != source
    assert relay_source.name.startswith(".z2m-parent-attachment-snapshot-")
    assert not relay_source.exists()


def test_full_text_attachment_service_skips_html_when_source_html_exists(
    tmp_path: Path,
) -> None:
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
        create_parent_attachment=lambda **kwargs: (
            create_calls.append(kwargs) or {"ok": True}
        ),
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
            "html_downloads": [
                {"ok": True, "output_path": str(source), "article": _article_html()}
            ],
            "pdf_downloads": [],
        },
    )

    assert result is not None
    assert result["skipped"] is True
    assert result["reason"] == "parent_already_has_source_html"
    assert result["existing_attachment_key"] == "HTML0001"
    assert create_calls == []


def test_full_text_attachment_service_trashes_dangling_source_html_before_attach(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    source.write_text(_native_article_html(), encoding="utf-8")
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
        create_parent_attachment=lambda **kwargs: (
            create_calls.append(kwargs) or {"ok": True, "newAttachmentKey": "HTML1234"}
        ),
        enqueue_pdf_for_ocr=lambda **_kwargs: {"unexpected": "ocr"},
        enqueue_pdf_for_html=lambda **_kwargs: {"unexpected": "html"},
        trash_source_html_attachment=lambda **kwargs: (
            trash_calls.append(kwargs) or {"ok": True, "trashed": True}
        ),
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
                    "file_path": str(
                        tmp_path / "storage" / "HTMLDEAD" / "Article [SOURCE HTML].html"
                    ),
                    "exists": False,
                }
            ],
        },
        payload={
            "html_downloads": [
                {"ok": True, "output_path": str(source), "article": _article_html()}
            ],
            "pdf_downloads": [],
        },
    )

    assert result is not None
    assert result["kind"] == "html"
    assert [call["attachment"].key for call in trash_calls] == ["HTMLDEAD"]
    assert trash_calls[0]["dry_run"] is False
    assert create_calls[0]["content_type"] == "text/html"
    assert result["source_html_cleanup"]["candidate_count"] == 1


def test_full_text_attachment_service_still_attaches_pdf_when_html_already_exists(
    tmp_path: Path,
) -> None:
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
        enqueue_pdf_for_html=lambda **_kwargs: {
            "ok": True,
            "classification": "skipped",
        },
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
            "html_downloads": [
                {"ok": True, "output_path": str(html), "article": _article_html()}
            ],
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
    source.write_text(_native_article_html(), encoding="utf-8")
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

    monkeypatch.setattr(
        full_text_attachment, "sync_parent_attachment_local", fail_local_metadata
    )

    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=lambda **_kwargs: {
            "ok": True,
            "newAttachmentKey": "HTML1234",
        },
        enqueue_pdf_for_ocr=lambda **_kwargs: {"unexpected": "ocr"},
        enqueue_pdf_for_html=lambda **_kwargs: {"unexpected": "html"},
    )

    result = service.attach(
        attachment=attachment,
        metadata=metadata,
        inventory={"attachments": []},
        payload={
            "html_downloads": [
                {"ok": True, "output_path": str(source), "article": _article_html()}
            ],
            "pdf_downloads": [],
        },
    )

    assert result is not None
    assert result["ok"] is True
    assert result["local_copy"]["ok"] is True
    assert result["local_metadata"]["ok"] is False
    assert result["local_metadata"]["reason"] == "local_metadata_failed"
    assert "database is locked" in result["local_metadata"]["error"]


def test_full_text_attachment_service_enqueues_attached_html_for_translation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    source.write_text(_native_article_html(), encoding="utf-8")
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
        create_parent_attachment=lambda **_kwargs: {
            "ok": True,
            "newAttachmentKey": "HTML1234",
        },
        enqueue_pdf_for_ocr=lambda **_kwargs: {"unexpected": "ocr"},
        enqueue_pdf_for_html=lambda **_kwargs: {"unexpected": "html"},
        enqueue_html_for_translation=lambda **kwargs: (
            translation_calls.append(kwargs) or {"ok": True, "queued": 1}
        ),
    )

    result = service.attach(
        attachment=attachment,
        metadata=metadata,
        inventory={"attachments": []},
        payload={
            "html_downloads": [
                {"ok": True, "output_path": str(source), "article": _article_html()}
            ],
            "pdf_downloads": [],
        },
    )

    assert result is not None
    assert result["translation_enqueue"] == {"ok": True, "queued": 1}
    assert translation_calls[0]["source_path"].name == "Article [SOURCE HTML].html"


def test_full_text_attachment_service_pdf_needing_ocr_uses_callback(
    tmp_path: Path,
) -> None:
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
        return {"ok": True, "classification": "queued"}

    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=lambda **_kwargs: {
            "ok": True,
            "newAttachmentKey": "PDF1234",
        },
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
    assert result["ocr_enqueue"] == {"ok": True, "classification": "queued"}
    assert ocr_calls[0]["source_path"].name == "Article [FULL TEXT].pdf"


def test_full_text_attachment_service_attaches_pdf_when_parent_only_has_html(
    tmp_path: Path,
) -> None:
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
        create_parent_attachment=lambda **kwargs: (
            create_calls.append(kwargs) or {"ok": True, "newAttachmentKey": "PDF1234"}
        ),
        enqueue_pdf_for_ocr=lambda **_kwargs: {"unexpected": "ocr"},
        enqueue_pdf_for_html=lambda **_kwargs: {
            "ok": True,
            "classification": "skipped",
        },
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


def test_full_text_attachment_service_attaches_html_and_pdf_when_both_found(
    tmp_path: Path,
) -> None:
    html = tmp_path / "article.html"
    html.write_text(_native_article_html(), encoding="utf-8")
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
        enqueue_pdf_for_html=lambda **_kwargs: {
            "ok": True,
            "classification": "skipped",
        },
    )

    result = service.attach(
        attachment=attachment,
        metadata=metadata,
        inventory={"has_html": False, "has_pdf": False, "attachments": []},
        payload={
            "html_downloads": [
                {"ok": True, "output_path": str(html), "article": _article_html()}
            ],
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
    assert [call["content_type"] for call in create_calls] == [
        "text/html",
        "application/pdf",
    ]
    attached_html = Path(result["local_copy"]["path"]).read_text(encoding="utf-8")
    assert 'id="web-doc"' in attached_html
    assert "This complete article paragraph" in attached_html
    assert Path(result["pdf_local_copy"]["path"]).read_bytes() == b"%PDF"


def test_full_text_attachment_service_skips_pdf_when_parent_has_pdf(
    tmp_path: Path,
) -> None:
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
        create_parent_attachment=lambda **kwargs: (
            create_calls.append(kwargs) or {"ok": True}
        ),
        enqueue_pdf_for_ocr=lambda **_kwargs: {"unexpected": "ocr"},
        enqueue_pdf_for_html=lambda **_kwargs: {"unexpected": "html"},
    )

    result = service.attach(
        attachment=attachment,
        metadata=metadata,
        inventory={"has_html": True, "has_pdf": True, "attachments": []},
        payload={
            "html_downloads": [],
            "pdf_downloads": [
                {"ok": True, "status": "downloaded", "output_path": str(source)}
            ],
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


def test_existing_standard_path_requires_valid_package_integrity(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    source.write_text(
        """
        <html><head><title>Article</title></head><body><article>
          <h1>Article</h1><p>Accepted article body.</p>
        </article></body></html>
        """,
        encoding="utf-8",
    )
    package = full_text_attachment.standardize_native_html_download(
        {
            "source": "publisher",
            "output_path": str(source),
            "article_verdict": {"ok": True, "text_chars": 12_000},
        },
        metadata=SimpleNamespace(title="Article"),
        package_root=tmp_path / "packages",
        source_context="test",
    )
    assert package["ok"] is True
    article_html = Path(package["article_html_path"])
    item = {
        "output_path": str(source),
        "standard_article_html_path": str(article_html),
        "standard_package": package,
    }

    assert (
        full_text_attachment._html_attachment_existing_standard_path(item)
        == article_html
    )

    article_html.write_text("tampered", encoding="utf-8")

    assert full_text_attachment._html_attachment_existing_standard_path(item) is None


def test_prepare_html_attachment_source_rejects_tampered_fresh_package(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    source.write_text(
        """
        <html><head><title>Article</title></head><body><article>
          <h1>Article</h1><p>Accepted article body.</p>
        </article></body></html>
        """,
        encoding="utf-8",
    )
    original_standardize = full_text_attachment.standardize_native_html_download

    def standardize_then_tamper(
        *args: object,
        **kwargs: object,
    ) -> dict[str, Any]:
        package = original_standardize(*args, **kwargs)
        assert package["ok"] is True
        Path(package["article_html_path"]).write_text("tampered", encoding="utf-8")
        return package

    monkeypatch.setattr(
        full_text_attachment,
        "standardize_native_html_download",
        standardize_then_tamper,
    )
    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=lambda **_kwargs: {"ok": True},
        enqueue_pdf_for_ocr=lambda **_kwargs: {"ok": True},
        enqueue_pdf_for_html=lambda **_kwargs: {"ok": True},
        allow_raw_html_fallback=True,
    )
    html = {
        "source": "publisher",
        "output_path": str(source),
        "article_verdict": {"ok": True, "text_chars": 12_000},
    }

    result = service._prepare_html_attachment_source(
        html=html,
        metadata=SimpleNamespace(title="Article"),
    )

    assert result["ok"] is False
    assert result["status"] == "article_package_integrity_failed"


@pytest.mark.parametrize(
    "malformed_package",
    [None, [], "invalid", True],
    ids=["none", "list", "string", "boolean"],
)
def test_prepare_html_attachment_source_rejects_non_mapping_standardizer_result(
    monkeypatch: Any,
    tmp_path: Path,
    malformed_package: object,
) -> None:
    source = tmp_path / "article.html"
    source.write_text(_native_article_html(), encoding="utf-8")
    html: dict[str, Any] = {
        "source": "publisher",
        "output_path": str(source),
        "article_verdict": {"ok": True, "text_chars": 12_000},
    }
    monkeypatch.setattr(
        full_text_attachment,
        "standardize_native_html_download",
        lambda *_args, **_kwargs: malformed_package,
    )
    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=lambda **_kwargs: {"ok": True},
        enqueue_pdf_for_ocr=lambda **_kwargs: {"ok": True},
        enqueue_pdf_for_html=lambda **_kwargs: {"ok": True},
        allow_raw_html_fallback=True,
    )

    result = service._prepare_html_attachment_source(
        html=html,
        metadata=SimpleNamespace(title="Article"),
    )

    assert result["ok"] is False
    assert result["status"] == "article_standard_invalid_result"
    assert result["sourcePath"] == str(source)
    assert result["source"] is html
    assert result["article_standard"] == {
        "ok": False,
        "reason": "article_standard_invalid_result",
        "error": (
            f"Expected a mapping result, got {type(malformed_package).__name__}."
        ),
    }
    assert html["standard_package"] == result["article_standard"]


def test_parent_attachment_local_copy_removes_owned_target_when_validation_fails(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.html"
    source.write_bytes(b"accepted source")
    expected = full_text_attachment._stable_file_fingerprint(
        source,
        max_bytes=128,
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    target = tmp_path / "storage" / "HTML1234" / "Article.html"
    original_fingerprint = full_text_attachment._stable_file_fingerprint

    def fail_published_target(path: Path, *, max_bytes: int) -> object:
        if path == target:
            raise OSError("published local copy unreadable")
        return original_fingerprint(path, max_bytes=max_bytes)

    monkeypatch.setattr(
        full_text_attachment,
        "_stable_file_fingerprint",
        fail_published_target,
    )

    with pytest.raises(OSError, match="published local copy unreadable"):
        full_text_attachment.write_parent_attachment_local_copy(
            attachment=attachment,
            source_path=source,
            filename="Article.html",
            relay_result={"newAttachmentKey": "HTML1234"},
            expected_source=expected,
        )

    assert not target.exists()
    assert not list(target.parent.glob("*.full-text-tmp-*"))


def test_parent_attachment_local_copy_cleanup_does_not_mask_copy_failure(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"PDF")
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    original_unlink = Path.unlink

    def copy_then_fail(
        _source_path: Path,
        target_path: Path,
        *,
        max_bytes: int | None,
    ) -> Any:
        del max_bytes
        Path(target_path).write_bytes(b"partial")
        raise OSError("local copy failed")

    def fail_temp_cleanup(path: Path, *args: object, **kwargs: object) -> None:
        if ".full-text-tmp-" in path.name:
            raise PermissionError("cleanup denied")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(full_text_attachment, "_copy_file_bounded", copy_then_fail)
    monkeypatch.setattr(Path, "unlink", fail_temp_cleanup)

    with pytest.raises(OSError, match="local copy failed"):
        full_text_attachment.write_parent_attachment_local_copy(
            attachment=attachment,
            source_path=source,
            filename="Article.pdf",
            relay_result={"newAttachmentKey": "PDF1234"},
        )

    target = tmp_path / "storage" / "PDF1234" / "Article.pdf"
    assert not target.exists()


def test_full_text_attachment_service_html_local_copy_failure_is_not_success(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    source.write_text(_native_article_html(), encoding="utf-8")
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    translation_calls: list[dict[str, Any]] = []

    def fail_local_copy(**_kwargs: Any) -> dict[str, Any]:
        raise OSError("local HTML copy failed")

    monkeypatch.setattr(
        full_text_attachment,
        "write_parent_attachment_local_copy",
        fail_local_copy,
    )
    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=lambda **_kwargs: {
            "ok": True,
            "newAttachmentKey": "HTML1234",
        },
        enqueue_pdf_for_ocr=lambda **_kwargs: {"ok": True},
        enqueue_pdf_for_html=lambda **_kwargs: {"ok": True},
        enqueue_html_for_translation=lambda **kwargs: (
            translation_calls.append(kwargs) or {"ok": True}
        ),
    )

    result = service.attach(
        attachment=attachment,
        metadata=metadata,
        inventory={"attachments": []},
        payload={
            "html_downloads": [
                {"ok": True, "output_path": str(source), "article": _article_html()}
            ],
            "pdf_downloads": [],
        },
    )

    assert result is not None
    assert result["ok"] is False
    assert result["status"] == "local_copy_failed"
    assert result["local_copy"]["reason"] == "local_copy_failed"
    assert "local HTML copy failed" in result["local_copy"]["error"]
    assert result["local_metadata"] == {
        "ok": False,
        "skipped": True,
        "reason": "local_copy_failed",
    }
    assert translation_calls == []


def test_full_text_attachment_service_pdf_local_copy_failure_is_not_success(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
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
    downstream_calls: list[dict[str, Any]] = []

    def fail_local_copy(**_kwargs: Any) -> dict[str, Any]:
        raise OSError("local PDF copy failed")

    monkeypatch.setattr(
        full_text_attachment,
        "write_parent_attachment_local_copy",
        fail_local_copy,
    )
    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=lambda **_kwargs: {
            "ok": True,
            "newAttachmentKey": "PDF1234",
        },
        enqueue_pdf_for_ocr=lambda **kwargs: (
            downstream_calls.append(kwargs) or {"ok": True}
        ),
        enqueue_pdf_for_html=lambda **kwargs: (
            downstream_calls.append(kwargs) or {"ok": True}
        ),
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
                    "output_path": str(source),
                    "identity": {"needs_ocr": False},
                }
            ],
        },
    )

    assert result is not None
    assert result["ok"] is False
    assert result["status"] == "local_copy_failed"
    assert result["local_copy"]["reason"] == "local_copy_failed"
    assert "local PDF copy failed" in result["local_copy"]["error"]
    assert result["pdf_enqueue"]["reason"] == "local_copy_failed"
    assert downstream_calls == []


def test_full_text_attachment_service_rejects_explicit_relay_failure_before_copy(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    source.write_text(_native_article_html(), encoding="utf-8")
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    local_copy_calls: list[dict[str, Any]] = []

    def record_local_copy(**kwargs: Any) -> dict[str, Any]:
        local_copy_calls.append(kwargs)
        return {"ok": True, "path": str(tmp_path / "unexpected.html")}

    monkeypatch.setattr(
        full_text_attachment,
        "write_parent_attachment_local_copy",
        record_local_copy,
    )
    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=lambda **_kwargs: {
            "ok": False,
            "error": "relay rejected attachment",
        },
        enqueue_pdf_for_ocr=lambda **_kwargs: {"ok": True},
        enqueue_pdf_for_html=lambda **_kwargs: {"ok": True},
    )

    result = service.attach(
        attachment=attachment,
        metadata=metadata,
        inventory={"attachments": []},
        payload={
            "html_downloads": [
                {"ok": True, "output_path": str(source), "article": _article_html()}
            ],
            "pdf_downloads": [],
        },
    )

    assert result is not None
    assert result["ok"] is False
    assert result["status"] == "relay_attachment_failed"
    assert result["relay"]["error"] == "relay rejected attachment"
    assert local_copy_calls == []


@pytest.mark.parametrize(
    "enqueue_result",
    [
        {"ok": False, "reason": "translation queue unavailable"},
        {"classification": "queued"},
    ],
    ids=["explicit-failure", "missing-ok-contract"],
)
def test_full_text_attachment_service_requires_successful_translation_enqueue(
    enqueue_result: dict[str, Any],
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    source.write_text(_native_article_html(), encoding="utf-8")
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=lambda **_kwargs: {
            "ok": True,
            "newAttachmentKey": "HTML1234",
        },
        enqueue_pdf_for_ocr=lambda **_kwargs: {"ok": True},
        enqueue_pdf_for_html=lambda **_kwargs: {"ok": True},
        enqueue_html_for_translation=lambda **_kwargs: dict(enqueue_result),
    )

    result = service.attach(
        attachment=attachment,
        metadata=metadata,
        inventory={"attachments": []},
        payload={
            "html_downloads": [
                {"ok": True, "output_path": str(source), "article": _article_html()}
            ],
            "pdf_downloads": [],
        },
    )

    assert result is not None
    assert result["ok"] is False
    assert result["status"] == "translation_enqueue_failed"
    assert result["translation_enqueue"] == enqueue_result
    assert result["local_copy"]["ok"] is True


@pytest.mark.parametrize(
    "enqueue_result",
    [
        {"ok": False, "reason": "PDF queue unavailable"},
        {"classification": "queued"},
    ],
    ids=["explicit-failure", "missing-ok-contract"],
)
def test_full_text_attachment_service_requires_successful_pdf_enqueue(
    enqueue_result: dict[str, Any],
    tmp_path: Path,
) -> None:
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
    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=lambda **_kwargs: {
            "ok": True,
            "newAttachmentKey": "PDF1234",
        },
        enqueue_pdf_for_ocr=lambda **_kwargs: dict(enqueue_result),
        enqueue_pdf_for_html=lambda **_kwargs: dict(enqueue_result),
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
                    "output_path": str(source),
                    "identity": {"needs_ocr": False},
                }
            ],
        },
    )

    assert result is not None
    assert result["ok"] is False
    assert result["status"] == "html_enqueue_failed"
    assert result["html_enqueue"] == enqueue_result
    assert result["local_copy"]["ok"] is True


@pytest.mark.parametrize("failed_kind", ["html", "pdf"])
def test_full_text_attachment_service_preserves_other_success_when_one_kind_fails(
    monkeypatch: Any,
    tmp_path: Path,
    failed_kind: str,
) -> None:
    html_source = tmp_path / "article.html"
    html_source.write_text(_native_article_html(), encoding="utf-8")
    pdf_source = tmp_path / "article.pdf"
    pdf_source.write_bytes(b"%PDF")
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    original_local_copy = full_text_attachment.write_parent_attachment_local_copy

    def selective_local_copy(**kwargs: Any) -> dict[str, Any]:
        filename = str(kwargs["filename"])
        if filename.endswith(".html") == (failed_kind == "html"):
            raise OSError(f"{failed_kind} local copy failed")
        return original_local_copy(**kwargs)

    monkeypatch.setattr(
        full_text_attachment,
        "write_parent_attachment_local_copy",
        selective_local_copy,
    )

    def create_parent_attachment(**kwargs: Any) -> dict[str, Any]:
        key = "HTML1234" if kwargs["content_type"] == "text/html" else "PDF1234"
        return {"ok": True, "newAttachmentKey": key}

    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=create_parent_attachment,
        enqueue_pdf_for_ocr=lambda **_kwargs: {"ok": True},
        enqueue_pdf_for_html=lambda **_kwargs: {"ok": True},
        enqueue_html_for_translation=lambda **_kwargs: {"ok": True},
    )

    result = service.attach(
        attachment=attachment,
        metadata=metadata,
        inventory={"has_html": False, "has_pdf": False, "attachments": []},
        payload={
            "html_downloads": [
                {
                    "ok": True,
                    "output_path": str(html_source),
                    "article": _article_html(),
                }
            ],
            "pdf_downloads": [
                {
                    "ok": True,
                    "output_path": str(pdf_source),
                    "identity": {"needs_ocr": False},
                }
            ],
        },
    )

    assert result is not None
    assert result["ok"] is True
    if failed_kind == "html":
        assert result["kind"] == "pdf"
        assert result["html_attachment"]["ok"] is False
        assert result["local_copy"]["ok"] is True
    else:
        assert result["kind"] == "html"
        assert result["pdf_attachment"]["ok"] is False
        assert result["local_copy"]["ok"] is True


@pytest.mark.parametrize("kind", ["html", "pdf"])
def test_full_text_attachment_service_reports_relay_exception_as_failure(
    monkeypatch: Any,
    tmp_path: Path,
    kind: str,
) -> None:
    source = tmp_path / f"article.{kind}"
    if kind == "html":
        source.write_text(_native_article_html(), encoding="utf-8")
        payload = {
            "html_downloads": [
                {
                    "ok": True,
                    "output_path": str(source),
                    "article": _article_html(),
                }
            ],
            "pdf_downloads": [],
        }
    else:
        source.write_bytes(b"%PDF")
        payload = {
            "html_downloads": [],
            "pdf_downloads": [
                {
                    "ok": True,
                    "output_path": str(source),
                    "identity": {"needs_ocr": False},
                }
            ],
        }
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    local_copy_calls: list[dict[str, Any]] = []

    def fail_relay(**_kwargs: Any) -> dict[str, Any]:
        raise ConnectionError("relay connection reset")

    def record_local_copy(**kwargs: Any) -> dict[str, Any]:
        local_copy_calls.append(kwargs)
        return {"ok": True, "path": str(tmp_path / "unexpected")}

    monkeypatch.setattr(
        full_text_attachment,
        "write_parent_attachment_local_copy",
        record_local_copy,
    )
    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=fail_relay,
        enqueue_pdf_for_ocr=lambda **_kwargs: {"ok": True},
        enqueue_pdf_for_html=lambda **_kwargs: {"ok": True},
        enqueue_html_for_translation=lambda **_kwargs: {"ok": True},
    )

    result = service.attach(
        attachment=attachment,
        metadata=metadata,
        inventory={"attachments": []},
        payload=payload,
    )

    assert result is not None
    assert result["ok"] is False
    assert result["status"] == "relay_attachment_failed"
    assert result["relay"]["reason"] == "relay_attachment_failed"
    assert "relay connection reset" in result["relay"]["error"]
    assert local_copy_calls == []


@pytest.mark.parametrize("kind", ["html", "pdf"])
@pytest.mark.parametrize(
    "relay_value",
    [None, [], "invalid relay result"],
    ids=["none", "list", "string"],
)
def test_full_text_attachment_service_rejects_non_mapping_relay_result(
    monkeypatch: Any,
    tmp_path: Path,
    kind: str,
    relay_value: object,
) -> None:
    source = tmp_path / f"article.{kind}"
    if kind == "html":
        source.write_text(_native_article_html(), encoding="utf-8")
        payload = {
            "html_downloads": [
                {
                    "ok": True,
                    "output_path": str(source),
                    "article": _article_html(),
                }
            ],
            "pdf_downloads": [],
        }
    else:
        source.write_bytes(b"%PDF")
        payload = {
            "html_downloads": [],
            "pdf_downloads": [
                {
                    "ok": True,
                    "output_path": str(source),
                    "identity": {"needs_ocr": False},
                }
            ],
        }
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    local_copy_calls: list[dict[str, Any]] = []

    def invalid_relay_result(**_kwargs: Any) -> object:
        return relay_value

    def record_local_copy(**kwargs: Any) -> dict[str, Any]:
        local_copy_calls.append(kwargs)
        return {"ok": True, "path": str(tmp_path / "unexpected")}

    monkeypatch.setattr(
        full_text_attachment,
        "write_parent_attachment_local_copy",
        record_local_copy,
    )
    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=invalid_relay_result,  # type: ignore[arg-type]
        enqueue_pdf_for_ocr=lambda **_kwargs: {"ok": True},
        enqueue_pdf_for_html=lambda **_kwargs: {"ok": True},
        enqueue_html_for_translation=lambda **_kwargs: {"ok": True},
    )

    result = service.attach(
        attachment=attachment,
        metadata=metadata,
        inventory={"attachments": []},
        payload=payload,
    )

    assert result is not None
    assert result["ok"] is False
    assert result["status"] == "relay_attachment_failed"
    assert result["relay"]["reason"] == "relay_attachment_invalid_result"
    assert "Expected a mapping result" in result["relay"]["error"]
    assert local_copy_calls == []


@pytest.mark.parametrize(
    "invalid_ok", ["false", 1], ids=["string-false", "integer-one"]
)
def test_article_verdict_requires_boolean_download_success(invalid_ok: object) -> None:
    verdict = html_download_article_verdict(
        {
            "ok": invalid_ok,
            "kind": "html",
            "url": "https://example.test/article",
            "output_path": "/tmp/article.html",
            "article": _article_html(),
        }
    )

    assert verdict["ok"] is False
    assert verdict["reason"] == "download_not_ok"


@pytest.mark.parametrize(
    "invalid_ok", ["false", 1], ids=["string-false", "integer-one"]
)
def test_article_verdict_rejects_non_boolean_validator_success(
    invalid_ok: object,
) -> None:
    article = _article_html()
    article["ok"] = invalid_ok

    verdict = html_download_article_verdict(
        {
            "ok": True,
            "kind": "html",
            "url": "https://example.test/article",
            "output_path": "/tmp/article.html",
            "article": article,
        }
    )

    assert verdict["ok"] is False
    assert verdict["reason"] == "article_validator_invalid_ok"


@pytest.mark.parametrize(
    "invalid_ok", ["false", 1], ids=["string-false", "integer-one"]
)
@pytest.mark.parametrize(
    ("payload_key", "suffix"),
    [("html_downloads", ".html"), ("pdf_downloads", ".pdf")],
    ids=["html", "pdf"],
)
def test_full_text_attachment_service_rejects_non_boolean_download_success(
    invalid_ok: object,
    payload_key: str,
    suffix: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / f"article{suffix}"
    if suffix == ".html":
        source.write_text(_native_article_html(), encoding="utf-8")
    else:
        source.write_bytes(b"%PDF")
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    relay_calls: list[dict[str, Any]] = []
    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=lambda **kwargs: (
            relay_calls.append(kwargs) or {"ok": True, "newAttachmentKey": "UNEXPECTED"}
        ),
        enqueue_pdf_for_ocr=lambda **_kwargs: {"ok": True},
        enqueue_pdf_for_html=lambda **_kwargs: {"ok": True},
        allow_raw_html_fallback=True,
    )
    download: dict[str, object] = {
        "ok": invalid_ok,
        "output_path": str(source),
    }
    if suffix == ".html":
        download["article"] = _article_html()
    payload = {
        "html_downloads": [],
        "pdf_downloads": [],
        payload_key: [download],
    }

    result = service.attach(
        attachment=attachment,
        metadata=metadata,
        inventory={"has_html": False, "has_pdf": False, "attachments": []},
        payload=payload,
    )

    assert result is None
    assert relay_calls == []


def test_pdf_attachment_binds_relay_and_local_copy_to_source_digest(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.pdf"
    source_bytes = b"%PDF-STABLE-CONTENT"
    source.write_bytes(source_bytes)
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    relay_call: dict[str, Any] = {}

    def create_parent_attachment(**kwargs: Any) -> dict[str, Any]:
        relay_call.update(kwargs)
        return {"ok": True, "newAttachmentKey": "PDF1234"}

    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=create_parent_attachment,
        enqueue_pdf_for_ocr=lambda **_kwargs: {"ok": True},
        enqueue_pdf_for_html=lambda **_kwargs: {"ok": True},
    )

    result = service.attach(
        attachment=attachment,
        metadata=metadata,
        inventory={"has_pdf": False, "attachments": []},
        payload={
            "html_downloads": [],
            "pdf_downloads": [
                {
                    "ok": True,
                    "output_path": str(source),
                    "identity": {"needs_ocr": False},
                }
            ],
        },
    )

    expected_sha256 = hashlib.sha256(source_bytes).hexdigest()
    assert result is not None
    assert result["ok"] is True
    assert relay_call["source_sha256"] == expected_sha256
    assert result["source_sha256"] == expected_sha256
    assert Path(result["local_copy"]["path"]).read_bytes() == source_bytes


def test_pdf_attachment_uses_pre_relay_snapshot_when_source_mutates(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.pdf"
    original_bytes = b"%PDF-ORIGINAL"
    source.write_bytes(original_bytes)
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")

    def mutate_source(**kwargs: Any) -> dict[str, Any]:
        relay_source = Path(kwargs["source_path"])
        assert relay_source != source
        assert relay_source.read_bytes() == original_bytes
        source.write_bytes(b"%PDF-MUTATED!")
        return {"ok": True, "newAttachmentKey": "PDF1234"}

    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=mutate_source,
        enqueue_pdf_for_ocr=lambda **_kwargs: {"ok": True},
        enqueue_pdf_for_html=lambda **_kwargs: {"ok": True},
    )

    result = service.attach(
        attachment=attachment,
        metadata=metadata,
        inventory={"has_pdf": False, "attachments": []},
        payload={
            "html_downloads": [],
            "pdf_downloads": [
                {
                    "ok": True,
                    "output_path": str(source),
                    "identity": {"needs_ocr": False},
                }
            ],
        },
    )

    assert result is not None
    assert result["ok"] is True
    assert Path(result["local_copy"]["path"]).read_bytes() == original_bytes
    assert source.read_bytes() == b"%PDF-MUTATED!"
    assert not list(tmp_path.rglob(".z2m-parent-attachment-snapshot-*"))


def test_html_attachment_uses_pre_relay_snapshot_when_source_mutates(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    original_text = _native_article_html()
    source.write_text(original_text, encoding="utf-8")
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    monkeypatch.setattr(
        full_text_attachment,
        "standardize_native_html_download",
        lambda *_args, **_kwargs: {"ok": False, "reason": "raw_snapshot_test"},
    )

    def mutate_source(**kwargs: Any) -> dict[str, Any]:
        relay_source = Path(kwargs["source_path"])
        assert relay_source != source
        assert relay_source.read_text(encoding="utf-8") == original_text
        source.write_text("<html><body>mutated</body></html>", encoding="utf-8")
        return {"ok": True, "newAttachmentKey": "HTML1234"}

    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=mutate_source,
        enqueue_pdf_for_ocr=lambda **_kwargs: {"ok": True},
        enqueue_pdf_for_html=lambda **_kwargs: {"ok": True},
        enqueue_html_for_translation=lambda **_kwargs: {"ok": True},
        allow_raw_html_fallback=True,
    )

    result = service.attach(
        attachment=attachment,
        metadata=metadata,
        inventory={"has_html": False, "attachments": []},
        payload={
            "html_downloads": [
                {
                    "ok": True,
                    "output_path": str(source),
                    "article": _article_html(),
                }
            ],
            "pdf_downloads": [],
        },
    )

    assert result is not None
    assert result["ok"] is True
    assert Path(result["local_copy"]["path"]).read_text(encoding="utf-8") == (
        original_text
    )
    assert source.read_text(encoding="utf-8") == ("<html><body>mutated</body></html>")
    assert not list(tmp_path.rglob(".z2m-parent-attachment-snapshot-*"))


def test_pdf_attachment_rejects_snapshot_mutation_after_relay_success(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.pdf"
    source.write_bytes(b"%PDF-ORIGINAL")
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")

    def mutate_snapshot(**kwargs: Any) -> dict[str, Any]:
        Path(kwargs["source_path"]).write_bytes(b"%PDF-MUTATED-SNAPSHOT")
        return {"ok": True, "newAttachmentKey": "PDF1234"}

    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=mutate_snapshot,
        enqueue_pdf_for_ocr=lambda **_kwargs: {"ok": True},
        enqueue_pdf_for_html=lambda **_kwargs: {"ok": True},
    )

    result = service.attach(
        attachment=attachment,
        metadata=metadata,
        inventory={"has_pdf": False, "attachments": []},
        payload={
            "html_downloads": [],
            "pdf_downloads": [
                {
                    "ok": True,
                    "output_path": str(source),
                    "identity": {"needs_ocr": False},
                }
            ],
        },
    )

    assert result is not None
    assert result["ok"] is False
    assert result["status"] == "attachment_snapshot_changed_after_relay"
    assert not (tmp_path / "storage" / "PDF1234").exists()
    assert not list(tmp_path.rglob(".z2m-parent-attachment-snapshot-*"))


def test_parent_attachment_snapshot_is_cleaned_on_relay_base_exception(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.pdf"
    source.write_bytes(b"%PDF-ORIGINAL")
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")

    def cancel_relay(**kwargs: Any) -> dict[str, Any]:
        snapshot = Path(kwargs["source_path"])
        assert snapshot.exists()
        raise KeyboardInterrupt("cancelled during relay")

    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=cancel_relay,
        enqueue_pdf_for_ocr=lambda **_kwargs: {"ok": True},
        enqueue_pdf_for_html=lambda **_kwargs: {"ok": True},
    )

    with pytest.raises(KeyboardInterrupt, match="cancelled during relay"):
        service.attach(
            attachment=attachment,
            metadata=metadata,
            inventory={"has_pdf": False, "attachments": []},
            payload={
                "html_downloads": [],
                "pdf_downloads": [
                    {
                        "ok": True,
                        "output_path": str(source),
                        "identity": {"needs_ocr": False},
                    }
                ],
            },
        )

    assert not list(tmp_path.rglob(".z2m-parent-attachment-snapshot-*"))


def test_parent_attachment_snapshot_is_cleaned_when_first_target_stat_fails(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.pdf"
    source.write_bytes(b"%PDF-ORIGINAL")
    expected_source = full_text_attachment._stable_file_fingerprint(
        source,
        max_bytes=None,
    )
    real_stat = Path.stat
    target_stat_failed = False

    def fail_first_published_snapshot_stat(
        path: Path,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        nonlocal target_stat_failed
        if (
            path.name.startswith(".z2m-parent-attachment-snapshot-")
            and not target_stat_failed
        ):
            target_stat_failed = True
            raise OSError("injected post-publish stat failure")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fail_first_published_snapshot_stat)

    with pytest.raises(OSError):
        full_text_attachment._create_parent_attachment_source_snapshot(
            source,
            expected_source=expected_source,
            max_bytes=None,
        )

    assert not list(tmp_path.rglob(".z2m-parent-attachment-snapshot-*"))
    assert not list(tmp_path.rglob("*.article-asset-tmp-*"))


@pytest.mark.parametrize(
    "attachment_key",
    ["../escape", "..\\escape", "nested/key", "."],
    ids=["parent", "windows-parent", "nested", "dot"],
)
def test_parent_attachment_local_copy_rejects_unsafe_relay_key(
    attachment_key: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.pdf"
    source.write_bytes(b"%PDF")
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")

    with pytest.raises(RuntimeError, match="usable attachment key"):
        full_text_attachment.write_parent_attachment_local_copy(
            attachment=attachment,
            source_path=source,
            filename="Article.pdf",
            relay_result={"newAttachmentKey": attachment_key},
        )

    assert not (tmp_path / "escape" / "Article.pdf").exists()


def test_parent_attachment_local_copy_rejects_symlinked_target_directory(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.pdf"
    source.write_bytes(b"%PDF")
    storage_dir = tmp_path / "storage"
    target_dir = storage_dir / "PDF1234"
    target_dir.mkdir(parents=True)
    attachment = SimpleNamespace(storage_dir=storage_dir)
    original_is_symlink = Path.is_symlink

    def fake_is_symlink(path: Path) -> bool:
        if path == target_dir:
            return True
        return original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)

    with pytest.raises(OSError, match="symlink"):
        full_text_attachment.write_parent_attachment_local_copy(
            attachment=attachment,
            source_path=source,
            filename="Article.pdf",
            relay_result={"newAttachmentKey": "PDF1234"},
        )

    assert not (target_dir / "Article.pdf").exists()


def test_parent_attachment_local_copy_uses_stable_streaming_copy(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.pdf"
    source.write_bytes(b"%PDF-STABLE")
    expected = full_text_attachment._stable_file_fingerprint(
        source,
        max_bytes=None,
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")

    def reject_unbounded_copy(*_args: object, **_kwargs: object) -> None:
        raise OSError("unbounded shutil.copy2 was used")

    monkeypatch.setattr(shutil, "copy2", reject_unbounded_copy)

    result = full_text_attachment.write_parent_attachment_local_copy(
        attachment=attachment,
        source_path=source,
        filename="Article.pdf",
        relay_result={"newAttachmentKey": "PDF1234"},
        expected_source=expected,
        max_source_bytes=None,
    )

    assert result["ok"] is True
    assert Path(result["path"]).read_bytes() == source.read_bytes()


@pytest.mark.parametrize(
    "invalid_needs_ocr",
    ["false", 1],
    ids=["string-false", "integer-one"],
)
def test_pdf_attachment_requires_exact_boolean_needs_ocr(
    invalid_needs_ocr: object,
    tmp_path: Path,
) -> None:
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
    html_calls: list[dict[str, Any]] = []
    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=lambda **_kwargs: {
            "ok": True,
            "newAttachmentKey": "PDF1234",
        },
        enqueue_pdf_for_ocr=lambda **kwargs: ocr_calls.append(kwargs) or {"ok": True},
        enqueue_pdf_for_html=lambda **kwargs: html_calls.append(kwargs) or {"ok": True},
    )

    result = service.attach(
        attachment=attachment,
        metadata=metadata,
        inventory={"has_pdf": False, "attachments": []},
        payload={
            "html_downloads": [],
            "pdf_downloads": [
                {
                    "ok": True,
                    "output_path": str(source),
                    "identity": {"needs_ocr": invalid_needs_ocr},
                }
            ],
        },
    )

    assert result is not None
    assert result["ok"] is True
    assert ocr_calls == []
    assert len(html_calls) == 1
    assert result["html_enqueue"]["ok"] is True


def test_parent_attachment_local_copy_rejects_non_string_relay_key(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.pdf"
    source.write_bytes(b"%PDF")
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")

    with pytest.raises(RuntimeError, match="usable attachment key"):
        full_text_attachment.write_parent_attachment_local_copy(
            attachment=attachment,
            source_path=source,
            filename="Article.pdf",
            relay_result={"newAttachmentKey": 1234},
        )

    assert not (tmp_path / "storage" / "1234" / "Article.pdf").exists()


def test_full_text_attachment_guard_blocks_relay_after_preparation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    source.write_text(_native_article_html(), encoding="utf-8")
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    guard_calls: list[str] = []
    relay_calls: list[dict[str, Any]] = []

    def ensure_active() -> None:
        guard_calls.append("check")
        if len(guard_calls) >= 2:
            raise RuntimeError("metadata lease lost")

    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=lambda **kwargs: (
            relay_calls.append(kwargs) or {"ok": True, "newAttachmentKey": "HTML1234"}
        ),
        enqueue_pdf_for_ocr=lambda **_kwargs: {"unexpected": "ocr"},
        enqueue_pdf_for_html=lambda **_kwargs: {"unexpected": "html"},
        ensure_active=ensure_active,
    )

    with pytest.raises(RuntimeError, match="metadata lease lost"):
        service.attach(
            attachment=attachment,
            metadata=metadata,
            inventory={"attachments": []},
            payload={
                "html_downloads": [
                    {
                        "ok": True,
                        "output_path": str(source),
                        "article": _article_html(),
                    }
                ],
                "pdf_downloads": [],
            },
        )

    assert guard_calls == ["check", "check"]
    assert relay_calls == []


def test_parent_attachment_local_copy_reuses_identical_target_without_replacing(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.pdf"
    source.write_bytes(b"%PDF-accepted")
    storage_dir = tmp_path / "storage"
    target = storage_dir / "PDF1234" / "Article.pdf"
    target.parent.mkdir(parents=True)
    target.write_bytes(source.read_bytes())
    before = full_text_attachment._stable_file_fingerprint(
        target,
        max_bytes=None,
    )

    result = full_text_attachment.write_parent_attachment_local_copy(
        attachment=SimpleNamespace(storage_dir=storage_dir),
        source_path=source,
        filename="Article.pdf",
        relay_result={"newAttachmentKey": "PDF1234"},
        max_source_bytes=None,
    )

    after = full_text_attachment._stable_file_fingerprint(
        target,
        max_bytes=None,
    )
    assert result["ok"] is True
    assert result["reused"] is True
    assert (after.device, after.inode) == (before.device, before.inode)
    assert target.read_bytes() == source.read_bytes()


def test_parent_attachment_local_copy_preserves_conflicting_existing_target(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.pdf"
    source.write_bytes(b"%PDF-new")
    storage_dir = tmp_path / "storage"
    target = storage_dir / "PDF1234" / "Article.pdf"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"%PDF-existing")

    with pytest.raises(FileExistsError, match="different content"):
        full_text_attachment.write_parent_attachment_local_copy(
            attachment=SimpleNamespace(storage_dir=storage_dir),
            source_path=source,
            filename="Article.pdf",
            relay_result={"newAttachmentKey": "PDF1234"},
            max_source_bytes=None,
        )

    assert target.read_bytes() == b"%PDF-existing"
    assert not list(target.parent.glob("*.full-text-tmp-*"))


def test_html_embedding_reports_malformed_resource_url_without_raising(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    source.write_text(
        '<html><body><img src="http://[broken/figure.png"></body></html>',
        encoding="utf-8",
    )

    embedded, info = full_text_attachment._html_attachment_source_with_embedded_assets(
        source
    )

    assert embedded == source
    assert info["failed"] is True
    assert info["reason"] == "unresolved_local_assets"
    assert info["unresolved_local_refs"] == ["http://[broken/figure.png"]


def test_full_text_attachment_rejects_non_string_pdf_output_path(
    tmp_path: Path,
) -> None:
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    relay_calls: list[dict[str, Any]] = []
    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=lambda **kwargs: (
            relay_calls.append(kwargs) or {"ok": True, "newAttachmentKey": "PDF1234"}
        ),
        enqueue_pdf_for_ocr=lambda **_kwargs: {"ok": True},
        enqueue_pdf_for_html=lambda **_kwargs: {"ok": True},
    )

    result = service.attach(
        attachment=attachment,
        metadata=metadata,
        inventory={"has_pdf": False, "attachments": []},
        payload={
            "html_downloads": [],
            "pdf_downloads": [
                {
                    "ok": True,
                    "output_path": 1,
                    "identity": {"needs_ocr": False},
                }
            ],
        },
    )

    assert result is None
    assert relay_calls == []


@pytest.mark.parametrize(
    ("markers", "section_markers"),
    [(1, []), ([], None), ({"article_tag": True}, "references")],
    ids=["integer-markers", "none-sections", "mapping-and-string"],
)
def test_html_download_score_ignores_non_list_marker_containers(
    markers: object,
    section_markers: object,
) -> None:
    score = full_text_attachment._html_download_score(
        {
            "kind": "landing",
            "output_path": "x",
            "article": {
                "markers": markers,
                "section_markers": section_markers,
                "text_chars": 100,
            },
        }
    )

    assert score == (0, 0, 100, -1, -1)


@pytest.mark.parametrize(
    "invalid_keep_key",
    [123, True, "../escape"],
    ids=["integer", "boolean", "unsafe-string"],
)
def test_full_text_attachment_rejects_invalid_cleanup_keep_key(
    monkeypatch: Any,
    tmp_path: Path,
    invalid_keep_key: object,
) -> None:
    source = tmp_path / "article.html"
    source.write_text(_native_article_html(), encoding="utf-8")
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    relay_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        full_text_attachment,
        "cleanup_source_html_inventory",
        lambda **_kwargs: {"ok": True, "keep_key": invalid_keep_key},
    )
    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=lambda **kwargs: (
            relay_calls.append(kwargs) or {"ok": True, "newAttachmentKey": "HTML1234"}
        ),
        enqueue_pdf_for_ocr=lambda **_kwargs: {"ok": True},
        enqueue_pdf_for_html=lambda **_kwargs: {"ok": True},
    )

    result = service.attach(
        attachment=SimpleNamespace(storage_dir=tmp_path / "storage"),
        metadata=metadata,
        inventory={"attachments": []},
        payload={
            "html_downloads": [
                {
                    "ok": True,
                    "output_path": str(source),
                    "article": _article_html(),
                }
            ],
            "pdf_downloads": [],
        },
    )

    assert result is not None
    assert result["ok"] is False
    assert result["status"] == "source_html_cleanup_invalid_result"
    assert result["source_html_cleanup"] == {
        "ok": True,
        "keep_key": invalid_keep_key,
    }
    assert relay_calls == []


def test_full_text_attachment_normalizes_non_mapping_local_metadata_result(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    source.write_text(_native_article_html(), encoding="utf-8")
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        title="Article",
    )
    monkeypatch.setattr(
        full_text_attachment,
        "sync_parent_attachment_local",
        lambda **_kwargs: ["malformed"],
    )
    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=lambda **_kwargs: {
            "ok": True,
            "newAttachmentKey": "HTML1234",
        },
        enqueue_pdf_for_ocr=lambda **_kwargs: {"ok": True},
        enqueue_pdf_for_html=lambda **_kwargs: {"ok": True},
    )

    result = service.attach(
        attachment=SimpleNamespace(storage_dir=tmp_path / "storage"),
        metadata=metadata,
        inventory={"attachments": []},
        payload={
            "html_downloads": [
                {
                    "ok": True,
                    "output_path": str(source),
                    "article": _article_html(),
                }
            ],
            "pdf_downloads": [],
        },
    )

    assert result is not None
    assert result["ok"] is True
    assert result["local_metadata"] == {
        "ok": False,
        "reason": "local_metadata_sync_invalid_result",
        "error": "Expected a mapping result, got list.",
    }


@pytest.mark.parametrize(
    "malformed_cleanup",
    [None, [], "invalid"],
    ids=["none", "list", "string"],
)
def test_full_text_attachment_normalizes_non_mapping_cleanup_result(
    monkeypatch: Any,
    tmp_path: Path,
    malformed_cleanup: object,
) -> None:
    source = tmp_path / "article.html"
    source.write_text(_native_article_html(), encoding="utf-8")
    monkeypatch.setattr(
        full_text_attachment,
        "cleanup_source_html_inventory",
        lambda **_kwargs: malformed_cleanup,
    )
    relay_calls: list[dict[str, Any]] = []
    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=lambda **kwargs: (
            relay_calls.append(kwargs) or {"ok": True, "newAttachmentKey": "HTML1234"}
        ),
        enqueue_pdf_for_ocr=lambda **_kwargs: {"ok": True},
        enqueue_pdf_for_html=lambda **_kwargs: {"ok": True},
    )

    result = service.attach(
        attachment=SimpleNamespace(storage_dir=tmp_path / "storage"),
        metadata=SimpleNamespace(
            library_id="LIB1",
            data_dir=tmp_path,
            key="ITEM1234",
            item_id=10,
            title="Article",
        ),
        inventory={"attachments": []},
        payload={
            "html_downloads": [
                {
                    "ok": True,
                    "output_path": str(source),
                    "article": _article_html(),
                }
            ],
            "pdf_downloads": [],
        },
    )

    assert result is not None
    assert result["ok"] is False
    assert result["status"] == "source_html_cleanup_invalid_result"
    assert result["source_html_cleanup"] == {
        "ok": False,
        "reason": "source_html_cleanup_invalid_result",
        "error": (
            f"Expected a mapping result, got {type(malformed_cleanup).__name__}."
        ),
    }
    assert relay_calls == []


@pytest.mark.parametrize(
    "malformed_local_metadata",
    [{"ok": "true"}, {"path": "attachment.pdf"}],
    ids=["string-ok", "missing-ok"],
)
def test_full_text_attachment_rejects_malformed_local_metadata_mapping(
    monkeypatch: Any,
    tmp_path: Path,
    malformed_local_metadata: dict[str, object],
) -> None:
    source = tmp_path / "article.pdf"
    source.write_bytes(b"%PDF")
    monkeypatch.setattr(
        full_text_attachment,
        "sync_parent_attachment_local",
        lambda **_kwargs: dict(malformed_local_metadata),
    )
    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=lambda **_kwargs: {
            "ok": True,
            "newAttachmentKey": "PDF1234",
        },
        enqueue_pdf_for_ocr=lambda **_kwargs: {"ok": True},
        enqueue_pdf_for_html=lambda **_kwargs: {"ok": True},
    )

    result = service.attach(
        attachment=SimpleNamespace(storage_dir=tmp_path / "storage"),
        metadata=SimpleNamespace(
            library_id="LIB1",
            data_dir=tmp_path,
            key="ITEM1234",
            item_id=10,
            title="Article",
        ),
        inventory={"has_pdf": False, "attachments": []},
        payload={
            "html_downloads": [],
            "pdf_downloads": [
                {
                    "ok": True,
                    "output_path": str(source),
                    "identity": {"needs_ocr": False},
                }
            ],
        },
    )

    assert result is not None
    assert result["ok"] is True
    assert result["local_metadata"] == {
        "ok": False,
        "reason": "local_metadata_sync_invalid_result",
        "error": "Expected a mapping result with an exact boolean ok field.",
    }


def test_full_text_attachment_guard_blocks_source_html_cleanup_side_effect(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    source.write_text(_native_article_html(), encoding="utf-8")
    guard_calls: list[str] = []
    trash_calls: list[dict[str, Any]] = []
    create_calls: list[dict[str, Any]] = []

    def ensure_active() -> None:
        guard_calls.append("check")
        if len(guard_calls) >= 2:
            raise RuntimeError("metadata lease lost")

    service = FullTextAttachmentService(
        relay_enabled=True,
        create_parent_attachment=lambda **kwargs: (
            create_calls.append(kwargs) or {"ok": True, "newAttachmentKey": "HTML1234"}
        ),
        enqueue_pdf_for_ocr=lambda **_kwargs: {"ok": True},
        enqueue_pdf_for_html=lambda **_kwargs: {"ok": True},
        trash_source_html_attachment=lambda **kwargs: (
            trash_calls.append(kwargs) or {"ok": True, "trashed": True}
        ),
        ensure_active=ensure_active,
    )

    with pytest.raises(RuntimeError, match="metadata lease lost"):
        service.attach(
            attachment=SimpleNamespace(storage_dir=tmp_path / "storage"),
            metadata=SimpleNamespace(
                library_id="LIB1",
                data_dir=tmp_path,
                key="ITEM1234",
                item_id=10,
                title="Article",
            ),
            inventory={
                "has_html": False,
                "has_source_html": False,
                "attachments": [
                    {
                        "key": "HTMLDEAD",
                        "content_type": "text/html",
                        "path": "storage:Article [SOURCE HTML].html",
                        "title": "Article [source HTML]",
                        "file_path": str(
                            tmp_path
                            / "storage"
                            / "HTMLDEAD"
                            / "Article [SOURCE HTML].html"
                        ),
                        "exists": False,
                    }
                ],
            },
            payload={
                "html_downloads": [
                    {
                        "ok": True,
                        "output_path": str(source),
                        "article": _article_html(),
                    }
                ],
                "pdf_downloads": [],
            },
        )

    assert guard_calls == ["check", "check"]
    assert trash_calls == []
    assert create_calls == []


def test_parent_attachment_snapshot_cleans_owned_file_when_validation_is_cancelled(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.pdf"
    source.write_bytes(b"%PDF-ORIGINAL")
    expected_source = full_text_attachment._stable_file_fingerprint(
        source,
        max_bytes=None,
    )
    original_fingerprint = full_text_attachment._stable_file_fingerprint

    def cancel_snapshot_validation(
        path: Path,
        *,
        max_bytes: int | None,
    ) -> Any:
        if path.name.startswith(".z2m-parent-attachment-snapshot-"):
            raise KeyboardInterrupt("cancel snapshot validation")
        return original_fingerprint(path, max_bytes=max_bytes)

    monkeypatch.setattr(
        full_text_attachment,
        "_stable_file_fingerprint",
        cancel_snapshot_validation,
    )

    with pytest.raises(KeyboardInterrupt, match="cancel snapshot validation"):
        full_text_attachment._create_parent_attachment_source_snapshot(
            source,
            expected_source=expected_source,
            max_bytes=None,
        )

    assert not list(tmp_path.glob(".z2m-parent-attachment-snapshot-*"))


def test_parent_attachment_snapshot_cleans_publish_completed_during_cancellation(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.pdf"
    source.write_bytes(b"%PDF-ORIGINAL")
    expected_source = full_text_attachment._stable_file_fingerprint(
        source,
        max_bytes=None,
    )
    original_replace = full_text_attachment.os.replace

    def replace_then_cancel(
        source_path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        target_path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    ) -> None:
        original_replace(source_path, target_path)
        if Path(target_path).name.startswith(".z2m-parent-attachment-snapshot-"):
            raise KeyboardInterrupt("cancel after snapshot publication")

    monkeypatch.setattr(full_text_attachment.os, "replace", replace_then_cancel)

    with pytest.raises(
        KeyboardInterrupt,
        match="cancel after snapshot publication",
    ):
        full_text_attachment._create_parent_attachment_source_snapshot(
            source,
            expected_source=expected_source,
            max_bytes=None,
        )

    assert not list(tmp_path.glob(".z2m-parent-attachment-snapshot-*"))
    assert not list(tmp_path.glob("*.article-asset-tmp-*"))
