from __future__ import annotations

import json
from pathlib import Path

from zotero_arxiv_html_ingest.html_fetch import validate_arxiv_html
from zotero_arxiv_html_ingest.models import ArxivCandidate, LocalAttachment
from zotero_arxiv_html_ingest.storage import arxiv_html_filename, write_arxiv_html_artifact


def test_validate_arxiv_html() -> None:
    valid = "<html><body>" + ("Article text. " * 30) + "</body></html>"

    assert validate_arxiv_html(valid, min_text_chars=100)["ok"] is True
    assert validate_arxiv_html("not html", min_text_chars=1)["reason"] == "missing_html_tag"
    assert validate_arxiv_html("<html><body>tiny</body></html>", min_text_chars=100)["reason"] == "too_little_text"


def test_arxiv_html_filename() -> None:
    assert arxiv_html_filename("paper.pdf") == "paper [ARXIV HTML].html"
    assert arxiv_html_filename("paper [ARXIV HTML].pdf") == "paper [ARXIV HTML].html"


def test_write_artifact(tmp_path: Path) -> None:
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
    candidate = ArxivCandidate(
        arxiv_id="2401.01234",
        score=1.0,
        title="A Careful Metadata Pipeline",
    )
    html_text = "<html><body>" + ("Article text. " * 30) + "</body></html>"
    validation = validate_arxiv_html(html_text, min_text_chars=100)

    artifact = write_arxiv_html_artifact(
        root=tmp_path / "html" / "arxiv",
        attachment=attachment,
        candidate=candidate,
        html_text=html_text,
        validation=validation,
    )

    assert artifact.path.name == "paper [ARXIV HTML].html"
    assert artifact.path.read_text(encoding="utf-8") == html_text
    manifest = json.loads(artifact.manifest_path.read_text(encoding="utf-8"))
    assert manifest["arxiv_id"] == "2401.01234"
    assert manifest["validation"]["ok"] is True

