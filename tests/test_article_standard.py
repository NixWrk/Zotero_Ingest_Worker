from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import zotero_ingest_worker.article_standard as article_standard_module
from zotero_ingest_worker.article_standard import (
    ARTICLE_HTML_STANDARD_VERSION,
    _article_html_with_standard_assets,
    evaluate_article_html,
    standardize_native_html_download,
)
from zotero_ingest_worker.full_text_attachment import _html_attachment_source_with_embedded_assets
from zoteropdf2md.html_contract import CANONICAL_HTML_PROFILE


def test_standardize_native_html_download_writes_article_package(tmp_path: Path) -> None:
    source = tmp_path / "01.publisher.example.html"
    assets = tmp_path / "01.publisher.example_assets"
    assets.mkdir()
    (assets / "figure.png").write_bytes(b"PNG")
    (assets / "unused-logo.png").write_bytes(b"LOGO")
    source.write_text(
        """
        <html>
          <head><title>Example Article</title><script src="runtime.js"></script></head>
          <body>
            <nav><img src="01.publisher.example_assets/unused-logo.png"></nav>
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
    normalized = article_html.read_text(encoding="utf-8")
    assert 'id="web-doc"' in normalized
    assert f'data-z2m-profile="{CANONICAL_HTML_PROFILE}"' in normalized
    assert "<script" not in normalized
    assert "unused-logo" not in normalized
    assert "assets/figure.png" in normalized
    assert 'src="data:image/png;base64,UE5H"' in normalized
    assert not (article_html.parent / "assets" / "figure.png").exists()
    assert not (article_html.parent / "assets" / "unused-logo.png").exists()
    raw_copy = article_html.parent / "source" / source.name
    assert "<script" in raw_copy.read_text(encoding="utf-8")

    manifest = json.loads(Path(package["manifest_path"]).read_text(encoding="utf-8"))
    quality = json.loads(Path(package["quality_path"]).read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    assert manifest["source"]["sha256"] == package["source_sha256"]
    assert manifest["integrity"]["article_html"]["sha256"] == package["article_sha256"]
    assert manifest["integrity"]["source_copy"]["bytes"] == source.stat().st_size
    assert manifest["integrity"]["quality"]["path"] == "quality.json"
    assert manifest["standard"] == ARTICLE_HTML_STANDARD_VERSION
    assert manifest["canonical"]["profile"] == CANONICAL_HTML_PROFILE
    assert manifest["canonical"]["status"] == "passed"
    assert manifest["canonical"]["provenance_kind"] == "generic_article"
    assert manifest["article"]["identifiers"]["doi"] == "10.1000/example"
    assert manifest["normalizer"]["polish"]["kind"] == "generic_article"
    assert manifest["normalizer"]["polish"]["article_extracted"] is False
    assert package["polish"]["inlined_images"] == 1
    assert quality["status"] == "passed"
    assert quality["canonical_contract"]["status"] == "passed"


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


def test_article_asset_copy_rejects_source_mutation_during_copy(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    source = tmp_path / "figure.bin"
    target = tmp_path / "copied.bin"
    source.write_bytes(b"A" * 2_097_152)
    original_open = Path.open
    mutation = {"done": False}

    class MutatingReader:
        def __init__(self, stream: Any) -> None:
            self._stream = stream

        def __enter__(self) -> MutatingReader:
            return self

        def __exit__(
            self,
            _exc_type: object,
            _exc: object,
            _traceback: object,
        ) -> None:
            self._stream.close()

        def fileno(self) -> int:
            return int(self._stream.fileno())

        def read(self, size: int = -1) -> bytes:
            payload = self._stream.read(size)
            if payload and not mutation["done"]:
                mutation["done"] = True
                with original_open(source, "r+b") as writer:
                    writer.seek(0)
                    writer.write(b"B" * 4096)
                    writer.flush()
                    article_standard_module.os.fsync(writer.fileno())
            return payload

    def open_with_mutation(
        path: Path,
        mode: str = "r",
        *args: object,
        **kwargs: object,
    ) -> Any:
        stream = original_open(path, mode, *args, **kwargs)
        if path == source and mode == "rb" and not mutation["done"]:
            return MutatingReader(stream)
        return stream

    monkeypatch.setattr(Path, "open", open_with_mutation)  # type: ignore[attr-defined]

    copied = article_standard_module._copy_file_bounded(
        source,
        target,
        max_bytes=2_097_152,
    )

    assert mutation["done"] is True
    assert copied is None
    assert not target.exists()


def test_article_asset_copy_temp_does_not_collide_with_real_asset(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    source_assets = tmp_path / "article_assets"
    source_assets.mkdir()
    collision = source_assets / ".figure.png.article-asset-tmp"
    figure = source_assets / "figure.png"
    collision.write_bytes(b"COLLISION")
    figure.write_bytes(b"FIGURE")
    source.write_text(
        '<html><body><img src="article_assets/.figure.png.article-asset-tmp">'
        '<img src="article_assets/figure.png"></body></html>',
        encoding="utf-8",
    )
    package_dir = tmp_path / "package"
    package_dir.mkdir()

    _html_text, assets = _article_html_with_standard_assets(
        source_html=source,
        package_dir=package_dir,
    )

    assert [asset["status"] for asset in assets] == ["copied", "copied"]
    assert (package_dir / "assets" / collision.name).read_bytes() == b"COLLISION"
    assert (package_dir / "assets" / figure.name).read_bytes() == b"FIGURE"


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


def test_standardize_native_html_rejects_non_article_before_creating_package(tmp_path: Path) -> None:
    source = tmp_path / "landing.html"
    source.write_text(
        "<html><head><title>Abstract</title></head><body>Download PDF</body></html>",
        encoding="utf-8",
    )

    result = standardize_native_html_download(
        {
            "source": "arxiv",
            "url": "https://arxiv.org/abs/2501.00001",
            "output_path": str(source),
            "article_verdict": {"ok": True, "text_chars": 12_000},
        },
        metadata=SimpleNamespace(title="Article"),
        package_root=tmp_path / "packages",
        source_context="test",
    )

    assert result["ok"] is False
    assert result["reason"] == "source_html_polish_failed"
    assert "WebHtmlPolishError" in result["error"]
    assert not (tmp_path / "packages").exists()


def test_article_package_copies_local_srcset_reference_with_iiif_commas(tmp_path: Path) -> None:
    source = tmp_path / "article.html"
    source_assets = tmp_path / "article_assets"
    nested = source_assets / "iiif" / "full" / "1234," / "0"
    nested.mkdir(parents=True)
    figure = nested / "default.jpg"
    figure.write_bytes(b"JPEG")
    source.write_text("<html></html>", encoding="utf-8")
    package_dir = tmp_path / "package"
    package_dir.mkdir()

    html_text, assets = _article_html_with_standard_assets(
        source_html=source,
        package_dir=package_dir,
        html_text=(
            '<picture><source srcset="article_assets/iiif/full/1234,/0/default.jpg 1x, '
            'article_assets/iiif/full/1234,/default.jpg 2x"></picture>'
        ),
    )

    assert "assets/iiif/full/1234,/0/default.jpg" in html_text
    assert assets[0]["status"] == "copied"
    copied = package_dir / "assets" / "iiif" / "full" / "1234," / "0" / "default.jpg"
    assert copied.read_bytes() == b"JPEG"


def test_article_quality_rejects_traversal_and_remote_svg_resources(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"SECRET")
    article = package / "article.html"
    article.write_text(
        """
        <html><head><title>Article</title></head><body><article>
          <img src="../outside.png">
          <svg><image href="https://cdn.example/figure.svg"></image></svg>
        </article></body></html>
        """,
        encoding="utf-8",
    )

    quality = evaluate_article_html(
        article_html=article,
        metadata=SimpleNamespace(title="Article"),
        source_download={},
        article_verdict={"ok": True, "text_chars": 12_000},
    )

    assert quality["status"] == "failed"
    assert "missing_local_resources" in quality["failures"]
    assert "remote_assets_present" in quality["failures"]
    assert "../outside.png" in quality["missing_local_resources"]
    assert quality["remote_asset_count"] == 1


def test_article_quality_rejects_unsafe_event_and_url_attributes(tmp_path: Path) -> None:
    article = tmp_path / "article.html"
    article.write_text(
        """
        <html><head><title>Article</title></head><body><article>
          <a href="javascript:alert(1)" onclick="alert(2)">unsafe</a>
        </article></body></html>
        """,
        encoding="utf-8",
    )

    quality = evaluate_article_html(
        article_html=article,
        metadata=SimpleNamespace(title="Article"),
        source_download={},
        article_verdict={"ok": True, "text_chars": 12_000},
    )

    assert "unsafe_attributes_present" in quality["failures"]
    assert quality["unsafe_attribute_count"] == 2


def test_standardize_native_html_reports_package_write_failure(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    source.write_text("<html><body><article>Article</article></body></html>", encoding="utf-8")
    monkeypatch.setattr(
        article_standard_module,
        "write_article_package",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    result = standardize_native_html_download(
        {"source": "publisher", "output_path": str(source)},
        metadata=SimpleNamespace(title="Article"),
        package_root=tmp_path / "packages",
        source_context="test",
    )

    assert result["ok"] is False
    assert result["reason"] == "article_package_write_failed"
    assert "disk full" in result["error"]


def test_standardize_native_html_preserves_committed_package_when_rebuild_fails(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    source.write_text(
        """
        <html><head><title>New Article</title></head><body><article>
          <h1>New Article</h1><p>Replacement article body.</p>
        </article></body></html>
        """,
        encoding="utf-8",
    )
    package_root = tmp_path / "packages"
    package_dir = package_root / "publisher.article"
    package_dir.mkdir(parents=True)
    previous_article = package_dir / "article.html"
    previous_manifest = package_dir / "manifest.json"
    previous_article.write_text("previous accepted article", encoding="utf-8")
    previous_manifest.write_text('{"accepted": true}', encoding="utf-8")
    original_copy2 = article_standard_module.shutil.copy2

    def fail_source_copy(
        source_path: object, target_path: object, *args: object, **kwargs: object
    ) -> object:
        if Path(target_path).parent.name == "source":
            raise OSError("simulated source-copy failure")
        return original_copy2(source_path, target_path, *args, **kwargs)

    monkeypatch.setattr(article_standard_module.shutil, "copy2", fail_source_copy)  # type: ignore[attr-defined]

    result = standardize_native_html_download(
        {
            "source": "publisher",
            "output_path": str(source),
            "article_verdict": {"ok": True, "text_chars": 12_000},
        },
        metadata=SimpleNamespace(title="New Article"),
        package_root=package_root,
        source_context="test",
    )

    assert result["ok"] is False
    assert result["reason"] == "article_package_write_failed"
    assert "simulated source-copy failure" in result["error"]
    assert previous_article.read_text(encoding="utf-8") == "previous accepted article"
    assert previous_manifest.read_text(encoding="utf-8") == '{"accepted": true}'
    assert not list(package_root.glob(".publisher.article.staging-*"))


def test_standardize_native_html_preserves_committed_package_on_quality_failure(
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    source.write_text(
        """
        <html><head><title>New Article</title></head><body><article>
          <h1>New Article</h1><p>Replacement article body.</p>
          <img src="https://cdn.example.test/remote.png">
        </article></body></html>
        """,
        encoding="utf-8",
    )
    package_root = tmp_path / "packages"
    package_dir = package_root / "publisher.article"
    package_dir.mkdir(parents=True)
    previous_article = package_dir / "article.html"
    previous_manifest = package_dir / "manifest.json"
    previous_article.write_text("previous accepted article", encoding="utf-8")
    previous_manifest.write_text('{"accepted": true}', encoding="utf-8")

    result = standardize_native_html_download(
        {
            "source": "publisher",
            "output_path": str(source),
            "article_verdict": {"ok": True, "text_chars": 12_000},
        },
        metadata=SimpleNamespace(title="New Article"),
        package_root=package_root,
        source_context="test",
    )

    assert result["ok"] is False
    assert "remote_assets_present" in result["quality_failures"]
    assert result["previous_package_retained"] is True
    assert "article_html_path" not in result
    assert previous_article.read_text(encoding="utf-8") == "previous accepted article"
    assert previous_manifest.read_text(encoding="utf-8") == '{"accepted": true}'
    assert not list(package_root.glob(".publisher.article.staging-*"))


def test_standardize_native_html_recovers_backup_before_failed_rebuild(
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
    package_root = tmp_path / "packages"
    download = {
        "source": "publisher",
        "output_path": str(source),
        "article_verdict": {"ok": True, "text_chars": 12_000},
    }
    accepted = standardize_native_html_download(
        download,
        metadata=SimpleNamespace(title="Article"),
        package_root=package_root,
        source_context="test",
    )
    assert accepted["ok"] is True
    package_dir = Path(accepted["package_dir"])
    accepted_article = (package_dir / "article.html").read_bytes()
    backup_dir = package_root / ".publisher.article.backup-interrupted"
    article_standard_module.os.replace(package_dir, backup_dir)

    monkeypatch.setattr(
        article_standard_module.shutil,
        "copy2",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("simulated reboot follow-up failure")
        ),
    )  # type: ignore[attr-defined]

    result = standardize_native_html_download(
        download,
        metadata=SimpleNamespace(title="Article"),
        package_root=package_root,
        source_context="test",
    )

    assert result["ok"] is False
    assert result["reason"] == "article_package_write_failed"
    assert (package_dir / "article.html").read_bytes() == accepted_article
    assert not backup_dir.exists()
    assert not list(package_root.glob(".publisher.article.staging-*"))


def test_article_package_promotion_does_not_clobber_concurrent_winner(
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
    package_root = tmp_path / "packages"
    accepted = standardize_native_html_download(
        {
            "source": "publisher",
            "output_path": str(source),
            "article_verdict": {"ok": True, "text_chars": 12_000},
        },
        metadata=SimpleNamespace(title="Article"),
        package_root=package_root,
        source_context="test",
    )
    assert accepted["ok"] is True
    package_dir = Path(accepted["package_dir"])
    staging_dir = package_root / ".publisher.article.staging-race"
    concurrent_dir = package_root / ".publisher.article.concurrent-winner"
    article_standard_module.shutil.copytree(package_dir, staging_dir)
    article_standard_module.shutil.copytree(package_dir, concurrent_dir)
    (staging_dir / "logs" / "writer.txt").write_text("our candidate", encoding="utf-8")
    (concurrent_dir / "logs" / "writer.txt").write_text(
        "concurrent winner", encoding="utf-8"
    )
    original_replace = article_standard_module.os.replace

    def race_replace(source_path: object, target_path: object) -> None:
        if Path(source_path) == staging_dir and Path(target_path) == package_dir:
            original_replace(concurrent_dir, package_dir)
            raise FileExistsError("simulated concurrent publication")
        original_replace(source_path, target_path)

    monkeypatch.setattr(article_standard_module.os, "replace", race_replace)  # type: ignore[attr-defined]

    try:
        article_standard_module._promote_article_package_tree(staging_dir, package_dir)
    except FileExistsError as exc:
        assert "simulated concurrent publication" in str(exc)
    else:
        raise AssertionError("Concurrent publication race did not fail this writer")

    assert (package_dir / "logs" / "writer.txt").read_text(encoding="utf-8") == (
        "concurrent winner"
    )
    assert staging_dir.exists()
    assert not list(package_root.glob(".publisher.article.backup-*"))


def test_standardize_native_html_rejects_source_mutation_during_build(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    package_root = tmp_path / "packages"
    download = {
        "source": "publisher",
        "output_path": str(source),
        "article_verdict": {"ok": True, "text_chars": 12_000},
    }
    source.write_text(
        """
        <html><head><title>Accepted</title></head><body><article>
          <h1>Accepted</h1><p>Previously accepted body.</p>
        </article></body></html>
        """,
        encoding="utf-8",
    )
    accepted = standardize_native_html_download(
        download,
        metadata=SimpleNamespace(title="Accepted"),
        package_root=package_root,
        source_context="test",
    )
    assert accepted["ok"] is True
    package_dir = Path(accepted["package_dir"])
    accepted_article = (package_dir / "article.html").read_bytes()

    source.write_text(
        """
        <html><head><title>Replacement</title></head><body><article>
          <h1>Replacement</h1><p>Replacement body.</p>
        </article></body></html>
        """,
        encoding="utf-8",
    )
    original_build_manifest = article_standard_module.build_article_manifest

    def mutate_source_during_build(
        *args: object, **kwargs: object
    ) -> dict[str, object]:
        manifest = original_build_manifest(*args, **kwargs)
        source.write_text(
            """
            <html><head><title>Mutated</title></head><body><article>
              <h1>Mutated</h1><p>Changed while the package was being built.</p>
            </article></body></html>
            """,
            encoding="utf-8",
        )
        return manifest

    monkeypatch.setattr(
        article_standard_module,
        "build_article_manifest",
        mutate_source_during_build,
    )

    result = standardize_native_html_download(
        download,
        metadata=SimpleNamespace(title="Replacement"),
        package_root=package_root,
        source_context="test",
    )

    assert result["ok"] is False
    assert result["reason"] == "article_package_write_failed"
    assert "source HTML changed" in result["error"]
    assert (package_dir / "article.html").read_bytes() == accepted_article


def test_article_package_completeness_rejects_modified_article_html(
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
    accepted = standardize_native_html_download(
        {
            "source": "publisher",
            "output_path": str(source),
            "article_verdict": {"ok": True, "text_chars": 12_000},
        },
        metadata=SimpleNamespace(title="Article"),
        package_root=tmp_path / "packages",
        source_context="test",
    )
    assert accepted["ok"] is True
    package_dir = Path(accepted["package_dir"])
    assert article_standard_module._article_package_tree_complete(package_dir) is True

    (package_dir / "article.html").write_text("tampered", encoding="utf-8")

    assert article_standard_module._article_package_tree_complete(package_dir) is False


def test_article_package_completeness_rejects_manifest_mismatch_and_extra_payload(
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
    accepted = standardize_native_html_download(
        {
            "source": "publisher",
            "output_path": str(source),
            "article_verdict": {"ok": True, "text_chars": 12_000},
        },
        metadata=SimpleNamespace(title="Article"),
        package_root=tmp_path / "packages",
        source_context="test",
    )
    assert accepted["ok"] is True
    package_dir = Path(accepted["package_dir"])

    source_mismatch = tmp_path / "source-mismatch"
    article_standard_module.shutil.copytree(package_dir, source_mismatch)
    manifest_path = source_mismatch / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source"]["sha256"] = "0" * 64
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    assert (
        article_standard_module._article_package_tree_complete(source_mismatch) is False
    )

    extra_payload = tmp_path / "extra-payload"
    article_standard_module.shutil.copytree(package_dir, extra_payload)
    (extra_payload / "unexpected.bin").write_bytes(b"not sealed")

    assert (
        article_standard_module._article_package_tree_complete(extra_payload) is False
    )

    malformed_assets = tmp_path / "malformed-assets"
    article_standard_module.shutil.copytree(package_dir, malformed_assets)
    malformed_manifest_path = malformed_assets / "manifest.json"
    malformed_manifest = json.loads(malformed_manifest_path.read_text(encoding="utf-8"))
    malformed_manifest["assets"].append("not-an-asset-record")
    malformed_manifest_path.write_text(
        json.dumps(malformed_manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    assert (
        article_standard_module._article_package_tree_complete(malformed_assets)
        is False
    )

    source_tamper = tmp_path / "source-tamper"
    article_standard_module.shutil.copytree(package_dir, source_tamper)
    source_tamper_manifest = json.loads(
        (source_tamper / "manifest.json").read_text(encoding="utf-8")
    )
    source_copy = (
        source_tamper / source_tamper_manifest["integrity"]["source_copy"]["path"]
    )
    source_copy.write_bytes(source_copy.read_bytes() + b"tampered")

    assert (
        article_standard_module._article_package_tree_complete(source_tamper) is False
    )

    legacy_schema = tmp_path / "legacy-schema"
    article_standard_module.shutil.copytree(package_dir, legacy_schema)
    legacy_manifest_path = legacy_schema / "manifest.json"
    legacy_manifest = json.loads(legacy_manifest_path.read_text(encoding="utf-8"))
    legacy_manifest["schema_version"] = 1
    legacy_manifest_path.write_text(
        json.dumps(legacy_manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    assert (
        article_standard_module._article_package_tree_complete(legacy_schema) is False
    )


def test_article_package_completeness_accepts_and_seals_copied_asset(
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
    source_download = {
        "source": "publisher",
        "output_path": str(source),
        "article_verdict": {"ok": True, "text_chars": 12_000},
    }
    metadata = SimpleNamespace(title="Article")
    accepted = standardize_native_html_download(
        source_download,
        metadata=metadata,
        package_root=tmp_path / "packages",
        source_context="test",
    )
    assert accepted["ok"] is True
    package_dir = Path(accepted["package_dir"])
    article_html = package_dir / "article.html"
    article_html.write_text(
        article_html.read_text(encoding="utf-8").replace(
            "</article>",
            '<img src="assets/figure.bin"></article>',
        ),
        encoding="utf-8",
    )
    asset = package_dir / "assets" / "figure.bin"
    asset.parent.mkdir(exist_ok=True)
    asset.write_bytes(b"SEALED-ASSET")
    quality = evaluate_article_html(
        article_html=article_html,
        metadata=metadata,
        source_download=source_download,
        article_verdict=source_download["article_verdict"],
    )
    assert quality["status"] in {"passed", "warning"}
    quality_path = package_dir / "quality.json"
    quality_path.write_text(
        json.dumps(quality, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    manifest_path = package_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assets = [
        {
            "path": "assets/figure.bin",
            "bytes": asset.stat().st_size,
            "sha256": article_standard_module.hashlib.sha256(
                asset.read_bytes()
            ).hexdigest(),
            "status": "copied",
        }
    ]
    source_copy_path = package_dir / manifest["integrity"]["source_copy"]["path"]
    manifest["canonical"] = quality["canonical_contract"]
    manifest["quality"] = {
        "status": quality["status"],
        "failures": quality["failures"],
        "warnings": quality["warnings"],
    }
    manifest["assets"] = assets
    manifest["integrity"] = article_standard_module._article_package_integrity_manifest(
        staging_dir=package_dir,
        article_html=article_html,
        source_copy=source_copy_path,
        quality_path=quality_path,
        assets=assets,
    )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    assert article_standard_module._article_package_tree_complete(package_dir) is True

    asset.write_bytes(b"TAMPERED-ASSET")

    assert article_standard_module._article_package_tree_complete(package_dir) is False


def test_article_package_completeness_hashes_exact_parsed_quality_snapshot(
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
    accepted = standardize_native_html_download(
        {
            "source": "publisher",
            "output_path": str(source),
            "article_verdict": {"ok": True, "text_chars": 12_000},
        },
        metadata=SimpleNamespace(title="Article"),
        package_root=tmp_path / "packages",
        source_context="test",
    )
    assert accepted["ok"] is True
    package_dir = Path(accepted["package_dir"])
    manifest_path = package_dir / "manifest.json"
    quality_path = package_dir / "quality.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    replacement_quality = json.loads(quality_path.read_text(encoding="utf-8"))
    replacement_quality["title"] = "Different bytes after parse"
    replacement_payload = json.dumps(
        replacement_quality,
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    manifest["integrity"]["quality"]["bytes"] = len(replacement_payload)
    manifest["integrity"]["quality"]["sha256"] = article_standard_module.hashlib.sha256(
        replacement_payload
    ).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    original_read_json = article_standard_module._read_json_object_bounded

    def read_then_replace_quality(
        path: Path,
        *,
        max_bytes: int,
    ) -> object:
        parsed = original_read_json(path, max_bytes=max_bytes)
        if path == quality_path:
            quality_path.write_bytes(replacement_payload)
        return parsed

    monkeypatch.setattr(
        article_standard_module,
        "_read_json_object_bounded",
        read_then_replace_quality,
    )

    assert article_standard_module._article_package_tree_complete(package_dir) is False
