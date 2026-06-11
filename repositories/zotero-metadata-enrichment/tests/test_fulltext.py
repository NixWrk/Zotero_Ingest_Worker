from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import zotero_metadata_enrichment.fulltext as fulltext
from zotero_metadata_enrichment.discovery import SourceDiscoveryResult
from zotero_metadata_enrichment.enrichment import EnricherConfig
from zotero_metadata_enrichment.models import FullTextLocation, LocalAttachment, LocalItemMetadata


def _metadata(tmp_path: Path) -> LocalItemMetadata:
    return LocalItemMetadata(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1234",
        item_id=10,
        version=1,
        item_type="journalArticle",
        date_modified=None,
        fields={"title": "A Careful Article"},
    )


def _attachment(tmp_path: Path) -> LocalAttachment:
    return LocalAttachment(
        library_id="LIB1",
        data_dir=tmp_path,
        storage_dir=tmp_path / "storage",
        key="ATTACH1",
        item_id=20,
        parent_item_id=10,
        parent_key="ITEM1234",
        file_path=tmp_path / "source.pdf",
    )


def _patch_discovery(monkeypatch: Any) -> FullTextLocation:
    location = FullTextLocation(source="test", url="https://example.test/article.pdf", kind="pdf")

    class FakeSourceDiscovery:
        def __init__(self, _config: EnricherConfig) -> None:
            pass

        def discover(
            self,
            *,
            metadata: LocalItemMetadata,
            attachment: LocalAttachment,
        ) -> SourceDiscoveryResult:
            return SourceDiscoveryResult(locations=[location])

    monkeypatch.setattr(fulltext, "SourceDiscovery", FakeSourceDiscovery)
    return location


def test_discovers_pdf_even_when_html_was_found(monkeypatch: Any, tmp_path: Path) -> None:
    location = _patch_discovery(monkeypatch)
    pdf_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        fulltext,
        "download_html_sources",
        lambda *_args, **_kwargs: [
            SimpleNamespace(ok=True, to_dict=lambda: {"ok": True, "output_path": "article.html"})
        ],
    )

    def fake_download_pdf_sources(locations: list[FullTextLocation], **kwargs: Any) -> list[SimpleNamespace]:
        pdf_calls.append({"locations": list(locations), **kwargs})
        return [
            SimpleNamespace(
                ok=True,
                identity={"needs_ocr": False},
                to_dict=lambda: {"ok": True, "output_path": "article.pdf"},
            )
        ]

    monkeypatch.setattr(fulltext, "download_pdf_sources", fake_download_pdf_sources)

    result = fulltext.discover_and_download_full_text(
        metadata=_metadata(tmp_path),
        attachment=_attachment(tmp_path),
        output_dir=tmp_path / "out",
        config=EnricherConfig(request_timeout_seconds=7, user_agent="test-agent"),
        max_pdf_downloads=2,
    )

    assert result.status == "html_and_pdf_found"
    assert result.html_downloads[0].ok is True
    assert result.pdf_downloads[0].ok is True
    assert pdf_calls[0]["locations"] == [location]
    assert pdf_calls[0]["limit"] == 2
    assert pdf_calls[0]["expected_title"] == "A Careful Article"


def test_discovers_pdf_derived_from_html_download(monkeypatch: Any, tmp_path: Path) -> None:
    _patch_discovery(monkeypatch)
    derived = FullTextLocation(
        source="europe_pmc",
        url="https://pmc.ncbi.nlm.nih.gov/articles/PMC12013345/pdf/nihms-2072483.pdf",
        kind="pdf",
    )
    pdf_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        fulltext,
        "download_html_sources",
        lambda *_args, **_kwargs: [
            SimpleNamespace(
                ok=True,
                derived_pdf_locations=[derived],
                to_dict=lambda: {"ok": True, "output_path": "article.html"},
            )
        ],
    )

    def fake_download_pdf_sources(locations: list[FullTextLocation], **kwargs: Any) -> list[SimpleNamespace]:
        pdf_calls.append({"locations": list(locations), **kwargs})
        return [
            SimpleNamespace(
                ok=True,
                identity={"needs_ocr": False},
                to_dict=lambda: {"ok": True, "output_path": "article.pdf"},
            )
        ]

    monkeypatch.setattr(fulltext, "download_pdf_sources", fake_download_pdf_sources)

    result = fulltext.discover_and_download_full_text(
        metadata=_metadata(tmp_path),
        attachment=_attachment(tmp_path),
        output_dir=tmp_path / "out",
        config=EnricherConfig(request_timeout_seconds=7, user_agent="test-agent"),
        max_pdf_downloads=2,
    )

    assert result.status == "html_and_pdf_found"
    assert pdf_calls[0]["locations"][-1] == derived


def test_skips_pdf_download_when_pdf_limit_is_zero(monkeypatch: Any, tmp_path: Path) -> None:
    _patch_discovery(monkeypatch)

    monkeypatch.setattr(
        fulltext,
        "download_html_sources",
        lambda *_args, **_kwargs: [
            SimpleNamespace(ok=True, to_dict=lambda: {"ok": True, "output_path": "article.html"})
        ],
    )

    def unexpected_pdf_download(*_args: Any, **_kwargs: Any) -> list[SimpleNamespace]:
        raise AssertionError("PDF download should be skipped when max_pdf_downloads is zero.")

    monkeypatch.setattr(fulltext, "download_pdf_sources", unexpected_pdf_download)

    result = fulltext.discover_and_download_full_text(
        metadata=_metadata(tmp_path),
        attachment=_attachment(tmp_path),
        output_dir=tmp_path / "out",
        config=EnricherConfig(),
        max_pdf_downloads=0,
    )

    assert result.status == "html_found"
    assert result.pdf_downloads == []
