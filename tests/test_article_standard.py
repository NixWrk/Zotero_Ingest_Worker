from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import zotero_ingest_worker.article_standard as article_standard_module
from zotero_ingest_worker.article_standard import (
    ARTICLE_HTML_STANDARD_VERSION,
    _article_html_with_standard_assets,
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


def test_standardize_native_html_rejects_symlink_source(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    source.write_text("<html><body>Article</body></html>", encoding="utf-8")
    original_is_symlink = Path.is_symlink

    def fake_is_symlink(path: Path) -> bool:
        return path == source or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)  # type: ignore[attr-defined]
    result = standardize_native_html_download(
        {
            "source": "publisher",
            "output_path": str(source),
            "article_verdict": {"ok": True},
        },
        metadata=SimpleNamespace(title="Article"),
        package_root=tmp_path / "packages",
        source_context="test",
    )

    assert result == {
        "ok": False,
        "reason": "source_html_symlink",
        "source_path": str(source),
    }


def test_standardize_native_html_rejects_oversized_source(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    source.write_text("<html><body>Article</body></html>", encoding="utf-8")
    monkeypatch.setattr(
        article_standard_module,
        "_file_size_or_zero",
        lambda _path: 16_000_001,
    )  # type: ignore[attr-defined]

    result = standardize_native_html_download(
        {"source": "publisher", "output_path": str(source)},
        metadata=SimpleNamespace(title="Article"),
        package_root=tmp_path / "packages",
        source_context="test",
    )

    assert result["ok"] is False
    assert result["reason"] == "source_html_too_large"
    assert result["source_bytes"] == 16_000_001
    assert not (tmp_path / "packages").exists()


def test_article_package_rejects_asset_candidate_outside_source_root(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    source_assets = tmp_path / "article_assets"
    source_assets.mkdir()
    source.write_text(
        '<html><body><img src="article_assets/host-secret.txt"></body></html>',
        encoding="utf-8",
    )
    outside = tmp_path / "host-secret.txt"
    outside.write_text("DO NOT COPY", encoding="utf-8")
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    monkeypatch.setattr(
        article_standard_module,
        "_source_asset_candidates",
        lambda *_args, **_kwargs: ([outside], False),
    )  # type: ignore[attr-defined]

    html_text, assets = _article_html_with_standard_assets(
        source_html=source,
        package_dir=package_dir,
    )

    assert "DO NOT COPY" not in html_text
    assert assets[0]["status"] == "skipped"
    assert assets[0]["reason"] == "asset_outside_root"
    assert not (package_dir / "assets" / "host-secret.txt").exists()


def test_article_package_skips_asset_over_byte_budget(tmp_path: Path) -> None:
    source = tmp_path / "article.html"
    source_assets = tmp_path / "article_assets"
    source_assets.mkdir()
    (source_assets / "figure.png").write_bytes(b"12345")
    source.write_text(
        '<html><body><img src="article_assets/figure.png"></body></html>',
        encoding="utf-8",
    )
    package_dir = tmp_path / "package"
    package_dir.mkdir()

    _html_text, assets = _article_html_with_standard_assets(
        source_html=source,
        package_dir=package_dir,
        max_asset_bytes=4,
    )

    assert assets[0]["status"] == "skipped"
    assert assets[0]["reason"] == "asset_too_large"
    assert not (package_dir / "assets" / "figure.png").exists()


def test_article_package_removes_stale_generated_assets(tmp_path: Path) -> None:
    source = tmp_path / "article.html"
    source_assets = tmp_path / "article_assets"
    source_assets.mkdir()
    figure = source_assets / "figure.png"
    figure.write_bytes(b"PNG")
    source.write_text(
        '<html><body><img src="article_assets/figure.png"></body></html>',
        encoding="utf-8",
    )
    package_dir = tmp_path / "package"
    package_dir.mkdir()

    _article_html_with_standard_assets(source_html=source, package_dir=package_dir)
    copied = package_dir / "assets" / "figure.png"
    assert copied.exists()

    figure.unlink()
    source.write_text("<html><body>Article</body></html>", encoding="utf-8")
    _article_html_with_standard_assets(source_html=source, package_dir=package_dir)

    assert not copied.exists()
