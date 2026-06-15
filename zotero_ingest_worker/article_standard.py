from __future__ import annotations

import html
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ARTICLE_HTML_STANDARD_VERSION = "article-html-standard/v1"
NATIVE_HTML_NORMALIZER_VERSION = "native-html-normalizer/v1"
ARTICLE_HTML_FILENAME = "article.html"
ASSETS_DIRNAME = "assets"


def standardize_native_html_download(
    download: dict[str, Any],
    *,
    metadata: Any,
    package_root: Path,
    source_context: str,
) -> dict[str, Any]:
    source_path = Path(str(download.get("output_path") or ""))
    verdict = download.get("article_verdict")
    verdict_data = verdict if isinstance(verdict, dict) else {}
    if not source_path.exists():
        return {
            "ok": False,
            "reason": "source_html_missing",
            "source_path": str(source_path),
        }
    package_dir = package_root / _package_dirname(download, source_path)
    return write_article_package(
        source_html=source_path,
        package_dir=package_dir,
        metadata=metadata,
        source_download=download,
        source_context=source_context,
        article_verdict=verdict_data,
    )


def write_article_package(
    *,
    source_html: Path,
    package_dir: Path,
    metadata: Any,
    source_download: dict[str, Any],
    source_context: str,
    article_verdict: dict[str, Any] | None = None,
) -> dict[str, Any]:
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "source").mkdir(exist_ok=True)
    (package_dir / "logs").mkdir(exist_ok=True)

    article_html = package_dir / ARTICLE_HTML_FILENAME
    source_copy = package_dir / "source" / source_html.name
    html_text, assets = _article_html_with_standard_assets(
        source_html=source_html,
        package_dir=package_dir,
    )
    article_html.write_text(html_text, encoding="utf-8")
    shutil.copy2(source_html, source_copy)

    quality = evaluate_article_html(
        article_html=article_html,
        metadata=metadata,
        source_download=source_download,
        article_verdict=article_verdict or {},
    )
    manifest = build_article_manifest(
        article_html=article_html,
        metadata=metadata,
        source_download=source_download,
        source_context=source_context,
        quality=quality,
        assets=assets,
    )
    manifest_path = package_dir / "manifest.json"
    quality_path = package_dir / "quality.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    quality_path.write_text(json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "ok": quality["status"] in {"passed", "warning"},
        "standard": ARTICLE_HTML_STANDARD_VERSION,
        "normalizer": NATIVE_HTML_NORMALIZER_VERSION,
        "package_dir": str(package_dir),
        "article_html_path": str(article_html),
        "manifest_path": str(manifest_path),
        "quality_path": str(quality_path),
        "quality_status": quality["status"],
        "quality_failures": quality["failures"],
    }


def build_article_manifest(
    *,
    article_html: Path,
    metadata: Any,
    source_download: dict[str, Any],
    source_context: str,
    quality: dict[str, Any],
    assets: list[dict[str, Any]],
) -> dict[str, Any]:
    identifiers = _metadata_identifiers(metadata)
    return {
        "standard": ARTICLE_HTML_STANDARD_VERSION,
        "normalizer": {
            "kind": "native_html",
            "version": NATIVE_HTML_NORMALIZER_VERSION,
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
        "article": {
            "title": _metadata_value(metadata, "title") or quality.get("title") or "",
            "authors": _metadata_authors(metadata),
            "identifiers": identifiers,
            "language": "",
        },
        "source": {
            "kind": "native_html",
            "context": source_context,
            "provider": str(source_download.get("source") or ""),
            "url": str(source_download.get("url") or ""),
            "final_url": str(source_download.get("final_url") or ""),
            "content_type": str(source_download.get("content_type") or ""),
            "original_output_path": str(source_download.get("output_path") or ""),
        },
        "files": {
            "article_html": article_html.name,
            "assets_dir": ASSETS_DIRNAME,
            "manifest": "manifest.json",
            "quality": "quality.json",
            "source_dir": "source",
            "logs_dir": "logs",
        },
        "assets": assets,
        "quality": {
            "status": quality["status"],
            "failures": quality["failures"],
            "warnings": quality["warnings"],
        },
    }


def evaluate_article_html(
    *,
    article_html: Path,
    metadata: Any,
    source_download: dict[str, Any],
    article_verdict: dict[str, Any],
) -> dict[str, Any]:
    text = article_html.read_text(encoding="utf-8", errors="replace")
    visible_text = _visible_text(text)
    article_text_chars = _int_value(article_verdict.get("text_chars"))
    text_chars = max(len(visible_text), article_text_chars)
    title = _html_title(text) or _metadata_value(metadata, "title")
    images = _html_attr_values(text, "img", "src")
    local_missing = _missing_local_refs(article_html.parent, images)
    internal_links = _internal_links(text)
    anchors = _anchors(text)
    missing_internal_links = sorted(
        link for link in internal_links if link and link not in anchors
    )[:50]
    remote_assets = [
        value
        for value in _resource_refs(text)
        if value.casefold().startswith(("http://", "https://", "//"))
    ]
    math = _math_strategy(text)

    failures: list[str] = []
    warnings: list[str] = []
    if not title:
        failures.append("missing_title")
    if article_verdict.get("ok") is False:
        failures.append(f"article_verdict_{article_verdict.get('reason') or 'failed'}")
    if text_chars < 4_000 and article_verdict.get("ok") is not True:
        failures.append("insufficient_text")
    if local_missing:
        failures.append("missing_local_images")
    if missing_internal_links:
        warnings.append("missing_internal_link_targets")
    if remote_assets:
        warnings.append("remote_assets_present")
    status = "failed" if failures else "warning" if warnings else "passed"
    return {
        "standard": ARTICLE_HTML_STANDARD_VERSION,
        "status": status,
        "title": title,
        "text_chars": text_chars,
        "image_count": len(images),
        "local_image_count": len(images) - len(local_missing),
        "missing_local_images": local_missing[:50],
        "remote_asset_count": len(remote_assets),
        "internal_link_count": len(internal_links),
        "missing_internal_links": missing_internal_links,
        "bibliography_anchor_count": _bibliography_anchor_count(anchors),
        "math_strategy": math,
        "article_verdict": article_verdict,
        "failures": failures,
        "warnings": warnings,
    }


def _article_html_with_standard_assets(
    *,
    source_html: Path,
    package_dir: Path,
) -> tuple[str, list[dict[str, Any]]]:
    html_text = source_html.read_text(encoding="utf-8", errors="replace")
    source_assets_dir = source_html.parent / f"{source_html.stem}_assets"
    target_assets_dir = package_dir / ASSETS_DIRNAME
    assets: list[dict[str, Any]] = []
    if not source_assets_dir.is_dir():
        return html_text, assets

    target_assets_dir.mkdir(exist_ok=True)
    for source in sorted(source_assets_dir.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(source_assets_dir)
        target = target_assets_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        assets.append(
            {
                "path": f"{ASSETS_DIRNAME}/{relative.as_posix()}",
                "bytes": target.stat().st_size,
            }
        )

    old_prefix = source_assets_dir.name
    if old_prefix != ASSETS_DIRNAME:
        quoted_prefix = re.escape(old_prefix)
        html_text = re.sub(
            rf"(?P<prefix>['\"(=\s]){quoted_prefix}/",
            rf"\g<prefix>{ASSETS_DIRNAME}/",
            html_text,
        )
    return html_text, assets


def _package_dirname(download: dict[str, Any], source_path: Path) -> str:
    provider = _safe_part(str(download.get("source") or "html"))
    stem = _safe_part(source_path.stem)
    return f"{provider}.{stem}"


def _metadata_value(metadata: Any, name: str) -> str:
    if isinstance(metadata, dict):
        return str(metadata.get(name) or "").strip()
    return str(getattr(metadata, name, "") or "").strip()


def _metadata_authors(metadata: Any) -> list[str]:
    raw = getattr(metadata, "authors", None)
    if isinstance(metadata, dict):
        raw = metadata.get("authors")
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(";") if part.strip()]
    return []


def _metadata_identifiers(metadata: Any) -> dict[str, str]:
    fields = ("doi", "pmid", "pmcid", "arxiv_id", "url")
    result: dict[str, str] = {}
    for field in fields:
        value = _metadata_value(metadata, field)
        if value:
            result[field] = value
    extra = _metadata_value(metadata, "extra")
    if extra:
        result["extra"] = extra
    return result


def _visible_text(text: str) -> str:
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _html_title(text: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.IGNORECASE | re.DOTALL)
    return _visible_text(match.group(1)) if match else ""


def _html_attr_values(text: str, tag: str, attr: str) -> list[str]:
    pattern = re.compile(
        rf"<{tag}\b[^>]*\b{attr}\s*=\s*(['\"])(.*?)\1",
        flags=re.IGNORECASE | re.DOTALL,
    )
    return [html.unescape(match.group(2)).strip() for match in pattern.finditer(text) if match.group(2).strip()]


def _resource_refs(text: str) -> list[str]:
    refs: list[str] = []
    for tag, attr in (("img", "src"), ("source", "src"), ("link", "href"), ("script", "src")):
        refs.extend(_html_attr_values(text, tag, attr))
    return refs


def _missing_local_refs(package_dir: Path, refs: list[str]) -> list[str]:
    missing: list[str] = []
    for value in refs:
        lowered = value.casefold()
        if not value or lowered.startswith(("http://", "https://", "//", "data:", "javascript:", "mailto:", "#")):
            continue
        path = Path(value.split("?", 1)[0].split("#", 1)[0])
        if not (package_dir / path).is_file():
            missing.append(value)
    return missing


def _internal_links(text: str) -> set[str]:
    return {
        urllib_fragment(value)
        for value in _html_attr_values(text, "a", "href")
        if value.startswith("#") and urllib_fragment(value)
    }


def _anchors(text: str) -> set[str]:
    pattern = re.compile(
        r"\b(?:id|name)\s*=\s*(['\"])(.*?)\1",
        flags=re.IGNORECASE | re.DOTALL,
    )
    return {html.unescape(match.group(2)).strip() for match in pattern.finditer(text) if match.group(2).strip()}


def urllib_fragment(value: str) -> str:
    return html.unescape(value[1:]).strip()


def _bibliography_anchor_count(anchors: set[str]) -> int:
    return sum(1 for anchor in anchors if re.search(r"(ref|bib|reference)", anchor, flags=re.IGNORECASE))


def _math_strategy(text: str) -> dict[str, Any]:
    lower = text.casefold()
    formula_images = len(
        re.findall(
            r"<img\b[^>]*(?:formula|math|equation|mml|tex)[^>]*>",
            text,
            flags=re.IGNORECASE,
        )
    )
    return {
        "mathml": "<math" in lower,
        "katex": "katex" in lower,
        "mathjax": "mathjax" in lower,
        "formula_image_count": formula_images,
    }


def _int_value(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_part(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return value.strip("._-")[:80] or "article"
