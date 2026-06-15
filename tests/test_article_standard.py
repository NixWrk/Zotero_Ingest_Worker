from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from zotero_ingest_worker.article_standard import (
    ARTICLE_HTML_STANDARD_VERSION,
    standardize_native_html_download,
)
from zotero_ingest_worker.full_text_attachment import _html_attachment_source_with_embedded_assets


def test_standardize_native_html_download_writes_article_package(tmp_path: Path) -> None:
    source = tmp_path / "01.publisher.example.html"
    assets = tmp_path / "01.publisher.example_assets"
    assets.mkdir()
    (assets / "figure.png").write_bytes(b"PNG")
    source.write_text(
        """
        <html>
          <head><title>Example Article</title></head>
          <body>
            <article id="article">
              <h1>Example Article</h1>
              <p>Full article body.</p>
              <img src="01.publisher.example_assets/figure.png">
              <a id="ref1"></a>
              <a href="#ref1">reference</a>
            </article>
          </body>
        </html>
        """,
        encoding="utf-8",
    )

    package = standardize_native_html_download(
        {
            "source": "publisher",
            "url": "https://publisher.example/article",
            "final_url": "https://publisher.example/article/full",
            "content_type": "text/html",
            "output_path": str(source),
            "article_verdict": {"ok": True, "text_chars": 12_000},
        },
        metadata=SimpleNamespace(title="Example Article", doi="10.1000/example"),
        package_root=tmp_path / "packages",
        source_context="parent_item",
    )

    assert package["ok"] is True
    article_html = Path(package["article_html_path"])
    assert article_html.name == "article.html"
    assert "assets/figure.png" in article_html.read_text(encoding="utf-8")
    assert (article_html.parent / "assets" / "figure.png").read_bytes() == b"PNG"

    manifest = json.loads(Path(package["manifest_path"]).read_text(encoding="utf-8"))
    quality = json.loads(Path(package["quality_path"]).read_text(encoding="utf-8"))
    assert manifest["standard"] == ARTICLE_HTML_STANDARD_VERSION
    assert manifest["article"]["identifiers"]["doi"] == "10.1000/example"
    assert quality["status"] == "passed"


def test_standard_article_html_assets_can_be_embedded_for_zotero_attachment(tmp_path: Path) -> None:
    package_dir = tmp_path / "package"
    assets = package_dir / "assets"
    assets.mkdir(parents=True)
    (assets / "figure.png").write_bytes(b"PNG")
    article = package_dir / "article.html"
    article.write_text('<html><body><img src="assets/figure.png"></body></html>', encoding="utf-8")

    embedded, info = _html_attachment_source_with_embedded_assets(article)

    assert info["enabled"] is True
    assert info["asset_count"] == 1
    assert embedded.read_text(encoding="utf-8").count("data:image/png;base64") == 1
