from __future__ import annotations

from pathlib import Path

from zotero_arxiv_html_ingest.ingest import ArxivHtmlIngestor, IngestConfig
from zotero_arxiv_html_ingest.models import ArxivCandidate, LocalAttachment


class FakeLookup:
    def by_id(self, arxiv_id: str) -> ArxivCandidate:
        return ArxivCandidate(arxiv_id=arxiv_id, score=1.0, title="Known")

    def by_title(self, title: str) -> ArxivCandidate:
        return ArxivCandidate(arxiv_id="2401.01234", score=1.0, title=title)


class FakeHtml:
    def fetch(self, arxiv_id: str) -> tuple[str, dict[str, object]]:
        return "<html><body>" + ("Article text. " * 30) + "</body></html>", {
            "ok": True,
            "reason": "ok",
            "text_chars": 420,
        }


def test_lookup_candidate_from_metadata_text(tmp_path: Path) -> None:
    ingestor = ArxivHtmlIngestor(
        IngestConfig(html_root=tmp_path, attach=False),
        lookup=FakeLookup(),  # type: ignore[arg-type]
        html=FakeHtml(),  # type: ignore[arg-type]
    )

    candidate = ingestor.lookup_candidate(metadata_text="arXiv:2401.01234")

    assert candidate is not None
    assert candidate.arxiv_id == "2401.01234"


def test_ingest_without_relay_writes_artifact(tmp_path: Path) -> None:
    source = tmp_path / "storage" / "PDF1234" / "paper.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"%PDF")
    attachment = LocalAttachment(
        library_id="LIB",
        data_dir=tmp_path,
        storage_dir=tmp_path / "storage",
        key="PDF1234",
        file_path=source,
    )
    ingestor = ArxivHtmlIngestor(
        IngestConfig(html_root=tmp_path / "html", attach=False),
        lookup=FakeLookup(),  # type: ignore[arg-type]
        html=FakeHtml(),  # type: ignore[arg-type]
    )

    result = ingestor.ingest(
        attachment=attachment,
        candidate=ArxivCandidate(arxiv_id="2401.01234", score=1.0),
    )

    assert result["ok"] is True
    artifact = result["artifact"]
    assert isinstance(artifact, dict)
    assert Path(str(artifact["path"])).is_file()

