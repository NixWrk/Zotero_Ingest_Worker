from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from zotero_ingest_worker.arxiv_html import ArxivHtmlJobService
from zotero_ingest_worker.local_zotero import LocalAttachment, LocalItemMetadata


def test_arxiv_html_service_looks_up_identifier_and_writes_manifest(tmp_path: Path) -> None:
    source_pdf = tmp_path / "paper.pdf"
    source_pdf.write_bytes(b"%PDF")
    metadata = LocalItemMetadata(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1",
        item_id=1,
        version=1,
        item_type="journalArticle",
        date_modified=None,
        fields={"title": "A Careful Metadata Pipeline", "extra": "arXiv:2401.01234 [cs.DL]"},
        creators=[],
        tags=[],
        collections=[],
        relations=[],
    )
    attachment = LocalAttachment(
        library_id="LIB1",
        data_dir=tmp_path,
        storage_dir=tmp_path / "storage",
        key="PDF1234",
        item_id=2,
        parent_item_id=1,
        parent_key="ITEM1",
        date_modified=None,
        link_mode=0,
        content_type="application/pdf",
        zotero_path="storage:paper.pdf",
        file_path=source_pdf,
    )
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:arxiv="http://arxiv.org/schemas/atom">
      <entry>
        <id>http://arxiv.org/abs/2401.01234v2</id>
        <updated>2024-01-03T00:00:00Z</updated>
        <published>2024-01-01T00:00:00Z</published>
        <title>A Careful Metadata Pipeline</title>
        <summary>This paper tests metadata.</summary>
        <author><name>Ada Lovelace</name></author>
        <arxiv:primary_category term="cs.DL" scheme="http://arxiv.org/schemas/atom"/>
      </entry>
    </feed>
    """
    seen_urls: list[str] = []

    def fake_http_text(url: str) -> str:
        seen_urls.append(url)
        return xml

    service = ArxivHtmlJobService(
        SimpleNamespace(
            arxiv_html_root=tmp_path / "arxiv",
            arxiv_search_min_score=0.88,
            arxiv_html_fetch_timeout_seconds=1,
            arxiv_html_min_text_chars=10,
            metadata_user_agent="test-agent",
            request_timeout_seconds=1,
        ),
        http_text=fake_http_text,
    )

    candidate = service.lookup_candidate(metadata=metadata, attachment=attachment)
    assert candidate is not None
    assert candidate.identifier == "2401.01234"
    assert service.provider_events[0]["provider"] == "arxiv"

    output_path = service.write_html_file(
        attachment=attachment,
        source_pdf=source_pdf,
        candidate=candidate,
        html_text="<html><body>article text</body></html>",
    )

    assert "id_list=2401.01234" in seen_urls[0]
    assert output_path.name == "paper [ARXIV HTML].html"
    manifest = json.loads((output_path.parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["job_kind"] == "arxiv_html"
    assert manifest["arxiv_id"] == "2401.01234"
