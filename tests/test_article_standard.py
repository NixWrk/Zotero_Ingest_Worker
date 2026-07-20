from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import zotero_ingest_worker.article_standard as article_standard_module
from zotero_ingest_worker.article_standard import (
    ARTICLE_HTML_STANDARD_VERSION,
    _article_html_with_standard_assets,
    evaluate_article_html,
    standardize_native_html_download,
)
from zotero_ingest_worker.full_text_attachment import (
    _html_attachment_source_with_embedded_assets,
)
from zoteropdf2md.html_contract import CANONICAL_HTML_PROFILE


def test_standardize_native_html_download_writes_article_package(
    tmp_path: Path,
) -> None:
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


def test_standard_article_html_assets_can_be_embedded_for_zotero_attachment(
    tmp_path: Path,
) -> None:
    package_dir = tmp_path / "package"
    assets = package_dir / "assets"
    assets.mkdir(parents=True)
    (assets / "figure.png").write_bytes(b"PNG")
    article = package_dir / "article.html"
    article.write_text(
        '<html><body><img src="assets/figure.png"></body></html>', encoding="utf-8"
    )

    embedded, info = _html_attachment_source_with_embedded_assets(article)

    assert info["enabled"] is True
    assert info["asset_count"] == 1
    assert embedded.read_text(encoding="utf-8").count("data:image/png;base64") == 1


def test_standard_article_embedding_preserves_sealed_package_payload(
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
            '<img src="assets/figure.png"></article>',
        ),
        encoding="utf-8",
    )
    asset = package_dir / "assets" / "figure.png"
    asset.parent.mkdir(exist_ok=True)
    asset.write_bytes(b"PNG")
    quality = evaluate_article_html(
        article_html=article_html,
        metadata=metadata,
        source_download=source_download,
        article_verdict=source_download["article_verdict"],
    )
    quality_path = package_dir / "quality.json"
    quality_path.write_text(
        json.dumps(quality, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    asset_record = {
        "path": "assets/figure.png",
        "bytes": asset.stat().st_size,
        "sha256": article_standard_module.hashlib.sha256(
            asset.read_bytes()
        ).hexdigest(),
        "status": "copied",
    }
    manifest_path = package_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["canonical"] = quality["canonical_contract"]
    manifest["quality"] = {
        "status": quality["status"],
        "failures": quality["failures"],
        "warnings": quality["warnings"],
    }
    manifest["assets"] = [asset_record]
    manifest["integrity"] = article_standard_module._article_package_integrity_manifest(
        staging_dir=package_dir,
        article_html=article_html,
        source_copy=package_dir / manifest["integrity"]["source_copy"]["path"],
        quality_path=quality_path,
        assets=[asset_record],
    )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    assert article_standard_module._article_package_tree_complete(package_dir) is True

    embedded, info = _html_attachment_source_with_embedded_assets(article_html)

    assert info["enabled"] is True
    assert embedded.parent == package_dir / "logs" / "attachment_snapshots"
    assert article_standard_module._article_package_tree_complete(package_dir) is True


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


def test_article_asset_scan_bounds_direct_source_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    source.write_bytes(b"123456789")
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    monkeypatch.setattr(
        article_standard_module,
        "ARTICLE_PACKAGE_MAX_SOURCE_BYTES",
        8,
    )

    with pytest.raises(OSError, match="exceeds 8 bytes"):
        _article_html_with_standard_assets(source_html=source, package_dir=package_dir)


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


def test_standardize_native_html_rejects_non_article_before_creating_package(
    tmp_path: Path,
) -> None:
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


def test_article_package_copies_local_srcset_reference_with_iiif_commas(
    tmp_path: Path,
) -> None:
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


def test_article_quality_rejects_traversal_and_remote_svg_resources(
    tmp_path: Path,
) -> None:
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


def test_article_quality_rejects_unsafe_event_and_url_attributes(
    tmp_path: Path,
) -> None:
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


def test_article_quality_reads_a_bounded_stable_snapshot(tmp_path: Path) -> None:
    article = tmp_path / "article.html"
    article.write_text(
        "<html><head><title>Article</title></head><body>"
        + "x" * 128
        + "</body></html>",
        encoding="utf-8",
    )

    with pytest.raises(OSError, match="exceeds 64 bytes"):
        evaluate_article_html(
            article_html=article,
            metadata=SimpleNamespace(title="Article"),
            source_download={},
            article_verdict={"ok": True, "text_chars": 12_000},
            max_bytes=64,
        )


def test_bounded_text_writer_removes_partial_file_on_encoding_failure(
    tmp_path: Path,
) -> None:
    output = tmp_path / "article.html"

    with pytest.raises(OSError, match="UTF-8"):
        article_standard_module._write_text_file_bounded(
            output,
            "prefix\ud800suffix",
            max_bytes=128,
        )

    assert not output.exists()


def test_bounded_text_writer_does_not_delete_preexisting_file(
    tmp_path: Path,
) -> None:
    output = tmp_path / "article.html"
    output.write_bytes(b"accepted")

    with pytest.raises(FileExistsError):
        article_standard_module._write_text_file_bounded(
            output,
            "replacement",
            max_bytes=128,
        )

    assert output.read_bytes() == b"accepted"


def test_bounded_text_writer_does_not_delete_concurrent_replacement(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    output = tmp_path / "article.html"
    winner = tmp_path / "winner.html"
    winner.write_bytes(b"accepted concurrent winner")

    def replace_before_validation(
        path: Path,
        *,
        max_bytes: int,
    ) -> object:
        assert max_bytes == 128
        os.replace(winner, path)
        raise OSError("validation failed")

    monkeypatch.setattr(
        article_standard_module,
        "_stable_file_fingerprint",
        replace_before_validation,
    )

    with pytest.raises(OSError, match="validation failed"):
        article_standard_module._write_text_file_bounded(output, "draft", max_bytes=128)

    assert output.read_bytes() == b"accepted concurrent winner"


@pytest.mark.parametrize(
    "reader_name",
    ["_read_file_snapshot_bounded", "_stable_file_fingerprint"],
)
def test_article_snapshot_rejects_descriptor_path_substitution(
    monkeypatch: Any,
    tmp_path: Path,
    reader_name: str,
) -> None:
    expected = tmp_path / "expected.html"
    substitute = tmp_path / "substitute.html"
    expected.write_bytes(b"A" * 64)
    substitute.write_bytes(b"B" * 64)
    original_open = Path.open

    def redirected_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        mode = str(args[0] if args else kwargs.get("mode") or "r")
        if path == expected and mode == "rb":
            return original_open(substitute, *args, **kwargs)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", redirected_open)
    reader = getattr(article_standard_module, reader_name)

    with pytest.raises(OSError, match="changed"):
        reader(expected, max_bytes=128)


def test_article_json_snapshot_rejects_descriptor_path_substitution(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    expected = tmp_path / "expected.json"
    substitute = tmp_path / "substitute.json"
    expected.write_text('{"safe":1}', encoding="utf-8")
    substitute.write_text('{"evil":1}', encoding="utf-8")
    original_open = Path.open

    def redirected_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        mode = str(args[0] if args else kwargs.get("mode") or "r")
        if path == expected and mode == "rb":
            return original_open(substitute, *args, **kwargs)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", redirected_open)

    assert (
        article_standard_module._read_json_object_bounded(
            expected,
            max_bytes=128,
        )
        is None
    )


def test_article_asset_copy_rejects_descriptor_path_substitution(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.bin"
    substitute = tmp_path / "substitute.bin"
    target = tmp_path / "target.bin"
    source.write_bytes(b"A" * 64)
    substitute.write_bytes(b"B" * 64)
    original_open = Path.open

    def redirected_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        mode = str(args[0] if args else kwargs.get("mode") or "r")
        if path == source and mode == "rb":
            return original_open(substitute, *args, **kwargs)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", redirected_open)

    copied = article_standard_module._copy_file_bounded(
        source,
        target,
        max_bytes=128,
    )

    assert copied is None
    assert not target.exists()
    assert not list(tmp_path.glob("*.article-asset-tmp-*"))


def test_standardize_native_html_reports_package_write_failure(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    source = tmp_path / "article.html"
    source.write_text(
        "<html><body><article>Article</article></body></html>", encoding="utf-8"
    )
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
    original_copy = article_standard_module._copy_file_bounded

    def fail_source_copy(
        source_path: Path,
        target_path: Path,
        *,
        max_bytes: int | None,
    ) -> Any:
        if Path(target_path).parent.name == "source":
            raise OSError("simulated source-copy failure")
        return original_copy(source_path, target_path, max_bytes=max_bytes)

    monkeypatch.setattr(
        article_standard_module,
        "_copy_file_bounded",
        fail_source_copy,
    )  # type: ignore[attr-defined]

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
        article_standard_module,
        "_copy_file_bounded",
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


def test_stat_identity_detects_ctime_change() -> None:
    before = SimpleNamespace(
        st_dev=1,
        st_ino=2,
        st_size=3,
        st_mtime_ns=4,
        st_ctime_ns=5,
    )
    after = SimpleNamespace(
        st_dev=1,
        st_ino=2,
        st_size=3,
        st_mtime_ns=4,
        st_ctime_ns=6,
    )

    assert article_standard_module._stat_identity(
        before,  # type: ignore[arg-type]
    ) != article_standard_module._stat_identity(
        after,  # type: ignore[arg-type]
    )


def test_article_package_source_copy_uses_bounded_streaming_copy(
    monkeypatch: Any,
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

    def reject_unbounded_copy(*_args: object, **_kwargs: object) -> None:
        raise OSError("unbounded shutil.copy2 was used")

    monkeypatch.setattr(article_standard_module.shutil, "copy2", reject_unbounded_copy)

    result = standardize_native_html_download(
        {
            "source": "publisher",
            "output_path": str(source),
            "article_verdict": {"ok": True, "text_chars": 12_000},
        },
        metadata=SimpleNamespace(title="Article"),
        package_root=tmp_path / "packages",
        source_context="test",
    )

    assert result["ok"] is True
    source_copy = Path(result["package_dir"]) / "source" / source.name
    assert source_copy.read_bytes() == source.read_bytes()


def test_article_package_promotion_rejects_post_publish_replacement_without_deleting_winner(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    staging_dir = tmp_path / ".package.staging-owner"
    package_dir = tmp_path / "package"
    concurrent_dir = tmp_path / ".package.concurrent-winner"
    displaced_dir = tmp_path / ".package.displaced-owner"
    staging_dir.mkdir()
    concurrent_dir.mkdir()
    (staging_dir / "owner.txt").write_text("our candidate", encoding="utf-8")
    (concurrent_dir / "owner.txt").write_text(
        "concurrent winner",
        encoding="utf-8",
    )
    original_replace = article_standard_module.os.replace
    swapped = False

    def complete_with_post_publish_replacement(path: Path) -> bool:
        nonlocal swapped
        if path == package_dir and not swapped:
            original_replace(package_dir, displaced_dir)
            original_replace(concurrent_dir, package_dir)
            swapped = True
        return True

    monkeypatch.setattr(
        article_standard_module,
        "_article_package_tree_complete",
        complete_with_post_publish_replacement,
    )

    with pytest.raises(OSError, match="ownership changed"):
        article_standard_module._promote_article_package_tree(
            staging_dir,
            package_dir,
        )

    assert swapped is True
    assert (package_dir / "owner.txt").read_text(encoding="utf-8") == (
        "concurrent winner"
    )
    assert (displaced_dir / "owner.txt").read_text(encoding="utf-8") == (
        "our candidate"
    )


def test_article_package_recovery_does_not_delete_replaced_backup(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    package_dir = tmp_path / "package"
    backup_dir = tmp_path / ".package.backup-interrupted"
    foreign_dir = tmp_path / ".package.foreign"
    displaced_dir = tmp_path / ".package.displaced-backup"
    package_dir.mkdir()
    backup_dir.mkdir()
    foreign_dir.mkdir()
    (package_dir / "owner.txt").write_text("accepted package", encoding="utf-8")
    (backup_dir / "owner.txt").write_text("our old backup", encoding="utf-8")
    (foreign_dir / "owner.txt").write_text("foreign backup", encoding="utf-8")
    original_replace = article_standard_module.os.replace
    swapped = False

    def complete_with_backup_replacement(path: Path) -> bool:
        nonlocal swapped
        if path == package_dir and not swapped:
            original_replace(backup_dir, displaced_dir)
            original_replace(foreign_dir, backup_dir)
            swapped = True
        return True

    monkeypatch.setattr(
        article_standard_module,
        "_article_package_tree_complete",
        complete_with_backup_replacement,
    )

    article_standard_module._recover_interrupted_article_package(package_dir)

    assert swapped is True
    assert (backup_dir / "owner.txt").read_text(encoding="utf-8") == ("foreign backup")
    assert (displaced_dir / "owner.txt").read_text(encoding="utf-8") == (
        "our old backup"
    )


def test_article_package_recovery_does_not_clobber_concurrent_package(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    package_dir = tmp_path / "package"
    backup_dir = tmp_path / ".package.backup-interrupted"
    concurrent_dir = tmp_path / ".package.concurrent-winner"
    displaced_dir = tmp_path / ".package.displaced-partial"
    package_dir.mkdir()
    backup_dir.mkdir()
    concurrent_dir.mkdir()
    (package_dir / "owner.txt").write_text("partial package", encoding="utf-8")
    (backup_dir / "owner.txt").write_text("recovery backup", encoding="utf-8")
    (concurrent_dir / "owner.txt").write_text(
        "concurrent winner",
        encoding="utf-8",
    )
    original_replace = article_standard_module.os.replace
    swapped = False

    def complete_with_package_replacement(path: Path) -> bool:
        nonlocal swapped
        if path == package_dir:
            return False
        if path == backup_dir and not swapped:
            original_replace(package_dir, displaced_dir)
            original_replace(concurrent_dir, package_dir)
            swapped = True
        return path == backup_dir

    monkeypatch.setattr(
        article_standard_module,
        "_article_package_tree_complete",
        complete_with_package_replacement,
    )

    article_standard_module._recover_interrupted_article_package(package_dir)

    assert swapped is True
    assert (package_dir / "owner.txt").read_text(encoding="utf-8") == (
        "concurrent winner"
    )
    assert (backup_dir / "owner.txt").read_text(encoding="utf-8") == ("recovery backup")
    assert (displaced_dir / "owner.txt").read_text(encoding="utf-8") == (
        "partial package"
    )


def test_unlimited_fingerprint_stops_after_initial_size_growth(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "growing.pdf"
    source.write_bytes(b"PDF0")
    original_open = Path.open
    growth_reads = 0

    class GrowingReader:
        def __init__(self, stream: Any) -> None:
            self.stream = stream

        def __enter__(self) -> GrowingReader:
            self.stream.__enter__()
            return self

        def __exit__(self, *args: object) -> object:
            return self.stream.__exit__(*args)

        def fileno(self) -> int:
            return self.stream.fileno()

        def read(self, size: int = -1) -> bytes:
            nonlocal growth_reads
            payload = self.stream.read(size)
            if payload:
                return payload
            growth_reads += 1
            if growth_reads <= 3:
                return b"X"
            return b""

    def open_with_growth(
        path: Path,
        mode: str = "r",
        *args: object,
        **kwargs: object,
    ) -> Any:
        stream = original_open(path, mode, *args, **kwargs)
        if path == source and mode == "rb":
            return GrowingReader(stream)
        return stream

    monkeypatch.setattr(Path, "open", open_with_growth)

    with pytest.raises(OSError, match="changed while fingerprinting"):
        article_standard_module._stable_file_fingerprint(
            source,
            max_bytes=None,
        )

    assert growth_reads == 1


def test_owned_article_package_removal_claims_path_before_recursive_delete(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    package_dir = tmp_path / "package"
    foreign_dir = tmp_path / ".package.foreign"
    displaced_dir = tmp_path / ".package.displaced-owner"
    package_dir.mkdir()
    foreign_dir.mkdir()
    (package_dir / "owner.txt").write_text("owned package", encoding="utf-8")
    (foreign_dir / "owner.txt").write_text("foreign package", encoding="utf-8")
    owner = article_standard_module._path_device_inode(package_dir)
    assert owner is not None
    original_replace = article_standard_module.os.replace
    original_rmtree = article_standard_module.shutil.rmtree
    swapped = False

    def replace_before_recursive_remove(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *args: object,
        **kwargs: object,
    ) -> None:
        nonlocal swapped
        target = Path(path)
        if target == package_dir:
            original_replace(package_dir, displaced_dir)
            original_replace(foreign_dir, package_dir)
            swapped = True
        original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(
        article_standard_module.shutil,
        "rmtree",
        replace_before_recursive_remove,
    )

    removed = article_standard_module._remove_owned_article_package_path(
        package_dir,
        owner=owner,
    )

    assert removed is True
    assert swapped is False
    assert not package_dir.exists()
    assert not displaced_dir.exists()
    assert (foreign_dir / "owner.txt").read_text(encoding="utf-8") == (
        "foreign package"
    )


def test_owned_article_package_removal_restores_replacement_claimed_during_rename(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    package_dir = tmp_path / "package"
    foreign_dir = tmp_path / ".package.foreign"
    displaced_dir = tmp_path / ".package.displaced-owner"
    package_dir.mkdir()
    foreign_dir.mkdir()
    (package_dir / "owner.txt").write_text("owned package", encoding="utf-8")
    (foreign_dir / "owner.txt").write_text("foreign package", encoding="utf-8")
    owner = article_standard_module._path_device_inode(package_dir)
    assert owner is not None
    original_rename = article_standard_module.os.rename
    original_replace = article_standard_module.os.replace
    swapped = False

    def replace_before_claim(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    ) -> None:
        nonlocal swapped
        if Path(source) == package_dir and not swapped:
            original_replace(package_dir, displaced_dir)
            original_replace(foreign_dir, package_dir)
            swapped = True
        original_rename(source, target)

    monkeypatch.setattr(
        article_standard_module.os,
        "rename",
        replace_before_claim,
    )

    removed = article_standard_module._remove_owned_article_package_path(
        package_dir,
        owner=owner,
    )

    assert removed is False
    assert swapped is True
    assert (package_dir / "owner.txt").read_text(encoding="utf-8") == (
        "foreign package"
    )
    assert (displaced_dir / "owner.txt").read_text(encoding="utf-8") == (
        "owned package"
    )
    assert not list(tmp_path.glob(".*.remove-claim-*"))


@pytest.mark.parametrize(
    "failure",
    [PermissionError("delete blocked"), KeyboardInterrupt()],
    ids=["os-error", "base-exception"],
)
def test_owned_article_package_removal_restores_claim_when_delete_fails(
    monkeypatch: Any,
    tmp_path: Path,
    failure: BaseException,
) -> None:
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    (package_dir / "owner.txt").write_text("owned package", encoding="utf-8")
    owner = article_standard_module._path_device_inode(package_dir)
    assert owner is not None

    def fail_recursive_remove(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(
        article_standard_module.shutil,
        "rmtree",
        fail_recursive_remove,
    )

    with pytest.raises(type(failure)):
        article_standard_module._remove_owned_article_package_path(
            package_dir,
            owner=owner,
        )

    assert article_standard_module._path_device_inode(package_dir) == owner
    assert (package_dir / "owner.txt").read_text(encoding="utf-8") == ("owned package")
    assert not list(tmp_path.glob(".*.remove-claim-*"))


def test_owned_article_package_removal_rejects_silent_delete_noop(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    (package_dir / "owner.txt").write_text("owned package", encoding="utf-8")
    owner = article_standard_module._path_device_inode(package_dir)
    assert owner is not None

    monkeypatch.setattr(
        article_standard_module.shutil,
        "rmtree",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(OSError, match="did not remove its owned claim"):
        article_standard_module._remove_owned_article_package_path(
            package_dir,
            owner=owner,
        )

    assert article_standard_module._path_device_inode(package_dir) == owner
    assert (package_dir / "owner.txt").read_text(encoding="utf-8") == ("owned package")
    assert not list(tmp_path.glob(".*.remove-claim-*"))


def test_owned_article_package_removal_preserves_claim_when_restore_is_occupied(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    (package_dir / "owner.txt").write_text("owned package", encoding="utf-8")
    owner = article_standard_module._path_device_inode(package_dir)
    assert owner is not None

    def fail_after_competing_package_appears(
        *_args: object,
        **_kwargs: object,
    ) -> None:
        package_dir.mkdir()
        (package_dir / "owner.txt").write_text(
            "competing package",
            encoding="utf-8",
        )
        raise PermissionError("delete blocked")

    monkeypatch.setattr(
        article_standard_module.shutil,
        "rmtree",
        fail_after_competing_package_appears,
    )

    with pytest.raises(PermissionError, match="delete blocked") as captured:
        article_standard_module._remove_owned_article_package_path(
            package_dir,
            owner=owner,
        )

    assert (package_dir / "owner.txt").read_text(encoding="utf-8") == (
        "competing package"
    )
    claims = list(tmp_path.glob(".*.remove-claim-*"))
    assert len(claims) == 1
    claim_path = claims[0]
    assert article_standard_module._path_device_inode(claim_path) == owner
    assert (claim_path / "owner.txt").read_text(encoding="utf-8") == ("owned package")
    notes = getattr(captured.value, "__notes__", [])
    assert any(str(claim_path) in note and str(package_dir) in note for note in notes)
