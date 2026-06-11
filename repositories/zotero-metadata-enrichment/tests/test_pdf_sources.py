from __future__ import annotations

from pathlib import Path
from typing import Any

from zotero_metadata_enrichment.models import FullTextLocation
from zotero_metadata_enrichment.pdf_sources import download_pdf_sources, fetch_pdf_source


class FakeResponse:
    def __init__(self, *, url: str, content_type: str, body: bytes) -> None:
        self.url = url
        self.headers = {"Content-Type": content_type}
        self._body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        return self._body


def test_fetch_pdf_source_saves_confirmed_pdf(monkeypatch: Any, tmp_path: Path) -> None:
    def fake_urlopen(request: Any, timeout: int) -> FakeResponse:
        assert request.full_url == "https://repo.example/paper.pdf"
        assert timeout == 10
        return FakeResponse(
            url="https://repo.example/paper.pdf",
            content_type="application/pdf",
            body=b"%PDF-1.7 article",
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = fetch_pdf_source(
        FullTextLocation(source="unpaywall", url="https://repo.example/paper.pdf", kind="pdf"),
        output_dir=tmp_path,
        timeout_seconds=10,
    )

    assert result.ok
    assert result.status == "downloaded"
    assert Path(result.output_path).read_bytes().startswith(b"%PDF")


def test_download_pdf_sources_zero_limit_skips_all(monkeypatch: Any, tmp_path: Path) -> None:
    def fake_urlopen(_request: Any, timeout: int) -> FakeResponse:
        raise AssertionError("limit=0 must not probe PDF URLs")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    results = download_pdf_sources(
        [
            FullTextLocation(source="unpaywall", url="https://repo.example/paper.pdf", kind="pdf"),
            FullTextLocation(source="crossref", url="https://repo.example/other.pdf", kind="pdf"),
        ],
        output_dir=tmp_path,
        limit=0,
    )

    assert results == []
    assert list(tmp_path.iterdir()) == []


def test_fetch_pdf_source_rejects_html_response(monkeypatch: Any, tmp_path: Path) -> None:
    def fake_urlopen(_request: Any, timeout: int) -> FakeResponse:
        return FakeResponse(
            url="https://repo.example/paper",
            content_type="text/html",
            body=b"<html><body>landing</body></html>",
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = fetch_pdf_source(
        FullTextLocation(source="crossref", url="https://repo.example/paper", kind="pdf"),
        output_dir=tmp_path,
    )

    assert not result.ok
    assert result.status == "non_pdf"
    assert list(tmp_path.iterdir()) == []


def test_fetch_pdf_source_rejects_unsafe_url(tmp_path: Path) -> None:
    result = fetch_pdf_source(
        FullTextLocation(source="bad", url="http://127.0.0.1/paper.pdf", kind="pdf"),
        output_dir=tmp_path,
    )

    assert not result.ok
    assert result.status == "unsafe_url"
    assert list(tmp_path.iterdir()) == []
