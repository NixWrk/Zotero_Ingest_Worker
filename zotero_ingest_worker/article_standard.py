from __future__ import annotations

import html
import json
import os
import re
import shutil
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from zoteropdf2md.web_html_polish import polish_web_html_file
from zoteropdf2md.web_polish.core import WebHtmlPolishError


ARTICLE_HTML_STANDARD_VERSION = "article-html-standard/v2"
NATIVE_HTML_NORMALIZER_VERSION = "native-html-normalizer/v2"
ARTICLE_HTML_FILENAME = "article.html"
ASSETS_DIRNAME = "assets"
ARTICLE_PACKAGE_MAX_SOURCE_BYTES = 16_000_000
ARTICLE_PACKAGE_MAX_ASSET_BYTES = 8_000_000
ARTICLE_PACKAGE_MAX_TOTAL_ASSET_BYTES = 64_000_000
ARTICLE_PACKAGE_MAX_ASSETS = 80
ARTICLE_PACKAGE_MAX_SCANNED_ASSETS = 512


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
    if source_path.is_symlink():
        return {
            "ok": False,
            "reason": "source_html_symlink",
            "source_path": str(source_path),
        }
    if not source_path.exists():
        return {
            "ok": False,
            "reason": "source_html_missing",
            "source_path": str(source_path),
        }
    source_bytes = _file_size_or_zero(source_path)
    if source_bytes > ARTICLE_PACKAGE_MAX_SOURCE_BYTES:
        return {
            "ok": False,
            "reason": "source_html_too_large",
            "source_path": str(source_path),
            "source_bytes": source_bytes,
        }
    package_root = package_root.resolve(strict=False)
    package_dir = package_root / _package_dirname(download, source_path)
    try:
        package_dir.resolve(strict=False).relative_to(package_root)
    except ValueError:
        return {
            "ok": False,
            "reason": "article_package_outside_root",
            "source_path": str(source_path),
        }
    try:
        return write_article_package(
            source_html=source_path,
            package_dir=package_dir,
            metadata=metadata,
            source_download=download,
            source_context=source_context,
            article_verdict=verdict_data,
        )
    except OSError as exc:
        return {
            "ok": False,
            "reason": "article_package_write_failed",
            "source_path": str(source_path),
            "error": f"{exc.__class__.__name__}: {exc}"[:500],
        }


def write_article_package(
    *,
    source_html: Path,
    package_dir: Path,
    metadata: Any,
    source_download: dict[str, Any],
    source_context: str,
    article_verdict: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_url = str(source_download.get("final_url") or source_download.get("url") or "")
    try:
        polished = polish_web_html_file(source_html, source_url=source_url or None)
    except (OSError, WebHtmlPolishError) as exc:
        return {
            "ok": False,
            "reason": "source_html_polish_failed",
            "source_path": str(source_html),
            "error": f"{exc.__class__.__name__}: {exc}"[:500],
        }
    if package_dir.is_symlink():
        return {
            "ok": False,
            "reason": "article_package_symlink",
            "source_path": str(source_html),
        }
    if package_dir.exists():
        shutil.rmtree(package_dir)
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "source").mkdir(exist_ok=True)
    (package_dir / "logs").mkdir(exist_ok=True)

    article_html = package_dir / ARTICLE_HTML_FILENAME
    source_copy = package_dir / "source" / source_html.name
    html_text, assets = _article_html_with_standard_assets(
        source_html=source_html,
        package_dir=package_dir,
        html_text=polished.html,
    )
    article_html.write_text(html_text, encoding="utf-8")
    shutil.copy2(source_html, source_copy)
    polish = _web_polish_manifest(polished)

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
        polish=polish,
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
        "polish": polish,
    }


def build_article_manifest(
    *,
    article_html: Path,
    metadata: Any,
    source_download: dict[str, Any],
    source_context: str,
    quality: dict[str, Any],
    assets: list[dict[str, Any]],
    polish: dict[str, Any],
) -> dict[str, Any]:
    identifiers = _metadata_identifiers(metadata)
    return {
        "standard": ARTICLE_HTML_STANDARD_VERSION,
        "normalizer": {
            "kind": "native_html",
            "version": NATIVE_HTML_NORMALIZER_VERSION,
            "polish": polish,
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
    resource_refs = _resource_refs(text)
    local_missing_images = _missing_local_refs(article_html.parent, images)
    local_missing_resources = _missing_local_refs(article_html.parent, resource_refs)
    internal_links = _internal_links(text)
    anchors = _anchors(text)
    missing_internal_links = sorted(
        link for link in internal_links if link and link not in anchors
    )[:50]
    remote_assets = [
        value
        for value in resource_refs
        if value.casefold().startswith(("http://", "https://", "//"))
    ]
    executable_payload_count = len(re.findall(r"<script\b", text, re.IGNORECASE))
    executable_payload_count += len(
        re.findall(
            r"<style\b(?![^>]*\bdata-z2m-style\s*=)", text, re.IGNORECASE
        )
    )
    active_media_count = len(
        re.findall(r"<(?:audio|video|iframe|object|embed)\b", text, re.IGNORECASE)
    )
    unsafe_attribute_count = _unsafe_attribute_count(text)
    math = _math_strategy(text)

    failures: list[str] = []
    warnings: list[str] = []
    if not title:
        failures.append("missing_title")
    if article_verdict.get("ok") is False:
        failures.append(f"article_verdict_{article_verdict.get('reason') or 'failed'}")
    if text_chars < 4_000 and article_verdict.get("ok") is not True:
        failures.append("insufficient_text")
    if local_missing_resources:
        failures.append("missing_local_resources")
    if remote_assets:
        failures.append("remote_assets_present")
    if executable_payload_count:
        failures.append("executable_payload_present")
    if active_media_count:
        failures.append("active_media_present")
    if unsafe_attribute_count:
        failures.append("unsafe_attributes_present")
    if missing_internal_links:
        warnings.append("missing_internal_link_targets")
    status = "failed" if failures else "warning" if warnings else "passed"
    return {
        "standard": ARTICLE_HTML_STANDARD_VERSION,
        "status": status,
        "title": title,
        "text_chars": text_chars,
        "image_count": len(images),
        "local_image_count": len(images) - len(local_missing_images),
        "missing_local_images": local_missing_images[:50],
        "missing_local_resources": local_missing_resources[:50],
        "remote_asset_count": len(remote_assets),
        "executable_payload_count": executable_payload_count,
        "active_media_count": active_media_count,
        "unsafe_attribute_count": unsafe_attribute_count,
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
    html_text: str | None = None,
    max_asset_bytes: int = ARTICLE_PACKAGE_MAX_ASSET_BYTES,
    max_total_asset_bytes: int = ARTICLE_PACKAGE_MAX_TOTAL_ASSET_BYTES,
    max_assets: int = ARTICLE_PACKAGE_MAX_ASSETS,
    max_scanned_assets: int = ARTICLE_PACKAGE_MAX_SCANNED_ASSETS,
) -> tuple[str, list[dict[str, Any]]]:
    if html_text is None:
        html_text = source_html.read_text(encoding="utf-8", errors="replace")
    source_assets_dir = source_html.parent / f"{source_html.stem}_assets"
    target_assets_dir = package_dir / ASSETS_DIRNAME
    assets: list[dict[str, Any]] = []
    _reset_generated_assets_dir(target_assets_dir)
    if not source_assets_dir.is_dir():
        return html_text, assets
    if source_assets_dir.is_symlink():
        assets.append(
            {
                "path": source_assets_dir.name,
                "bytes": 0,
                "status": "skipped",
                "reason": "assets_dir_symlink",
            }
        )
        return html_text, assets

    source_root = source_assets_dir.resolve()
    candidates, scan_truncated = _source_asset_candidates(
        source_assets_dir,
        html_text=html_text,
        max_scanned_assets=max_scanned_assets,
    )
    copied_count = 0
    total_bytes = 0
    for source in candidates:
        display_path = str(source)
        if source.is_symlink():
            assets.append(_skipped_asset(display_path, "asset_symlink"))
            continue
        try:
            relative = source.relative_to(source_assets_dir)
            resolved = source.resolve(strict=True)
            resolved.relative_to(source_root)
        except (OSError, ValueError):
            assets.append(_skipped_asset(display_path, "asset_outside_root"))
            continue
        if not resolved.is_file():
            assets.append(_skipped_asset(display_path, "asset_not_file"))
            continue
        size = _file_size_or_zero(resolved)
        if size > max(max_asset_bytes, 0):
            assets.append(_skipped_asset(relative.as_posix(), "asset_too_large", size=size))
            continue
        if copied_count >= max(max_assets, 0):
            assets.append(_skipped_asset(relative.as_posix(), "asset_count_limit", size=size))
            continue
        if total_bytes + size > max(max_total_asset_bytes, 0):
            assets.append(_skipped_asset(relative.as_posix(), "asset_total_bytes_limit", size=size))
            continue
        target = target_assets_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        copied_bytes = _copy_file_bounded(
            resolved,
            target,
            max_bytes=min(max(max_asset_bytes, 0), max(max_total_asset_bytes, 0) - total_bytes),
        )
        if copied_bytes is None:
            assets.append(_skipped_asset(relative.as_posix(), "asset_changed_or_too_large", size=size))
            continue
        copied_count += 1
        total_bytes += copied_bytes
        assets.append(
            {
                "path": f"{ASSETS_DIRNAME}/{relative.as_posix()}",
                "bytes": copied_bytes,
                "status": "copied",
            }
        )
    if scan_truncated:
        assets.append(_skipped_asset(source_assets_dir.name, "asset_scan_limit"))

    old_prefix = source_assets_dir.name
    if old_prefix != ASSETS_DIRNAME:
        quoted_prefix = re.escape(old_prefix)
        html_text = re.sub(
            rf"(?P<prefix>['\"(=\s]){quoted_prefix}/",
            rf"\g<prefix>{ASSETS_DIRNAME}/",
            html_text,
        )
    return html_text, assets


def _source_asset_candidates(
    source_assets_dir: Path,
    *,
    html_text: str | None = None,
    max_scanned_assets: int,
) -> tuple[list[Path], bool]:
    limit = max(max_scanned_assets, 0)
    if html_text is not None:
        return _referenced_source_asset_candidates(
            source_assets_dir,
            html_text=html_text,
            limit=limit,
        )
    files: list[Path] = []
    truncated = False
    for path in source_assets_dir.rglob("*"):
        if not path.is_file() and not path.is_symlink():
            continue
        if len(files) >= limit:
            truncated = True
            break
        files.append(path)
    files.sort(key=lambda path: str(path).casefold())
    return files, truncated


def _referenced_source_asset_candidates(
    source_assets_dir: Path,
    *,
    html_text: str,
    limit: int,
) -> tuple[list[Path], bool]:
    prefix = f"{source_assets_dir.name}/"
    candidates: list[Path] = []
    seen: set[str] = set()
    truncated = False
    for raw_ref in _resource_refs(html_text):
        ref = html.unescape(raw_ref).split("?", 1)[0].split("#", 1)[0]
        ref = urllib.parse.unquote(ref).replace("\\", "/")
        if not ref.startswith(prefix):
            continue
        relative = ref[len(prefix) :]
        parts = tuple(part for part in relative.split("/") if part)
        if not parts or any(part in {".", ".."} for part in parts):
            continue
        key = "/".join(parts)
        if key in seen:
            continue
        if len(candidates) >= limit:
            truncated = True
            break
        seen.add(key)
        candidates.append(source_assets_dir.joinpath(*parts))
    candidates.sort(key=lambda path: str(path).casefold())
    return candidates, truncated


def _reset_generated_assets_dir(target_assets_dir: Path) -> None:
    if target_assets_dir.is_symlink():
        target_assets_dir.unlink()
    elif target_assets_dir.exists():
        shutil.rmtree(target_assets_dir)


def _copy_file_bounded(source: Path, target: Path, *, max_bytes: int) -> int | None:
    limit = max(max_bytes, 0)
    temp = target.with_name(f".{target.name}.article-asset-tmp")
    copied = 0
    try:
        with source.open("rb") as source_stream, temp.open("wb") as target_stream:
            while True:
                chunk = source_stream.read(min(1_048_576, limit - copied + 1))
                if not chunk:
                    break
                copied += len(chunk)
                if copied > limit:
                    return None
                target_stream.write(chunk)
        shutil.copystat(source, temp)
        os.replace(temp, target)
        return copied
    finally:
        if temp.exists():
            temp.unlink()


def _skipped_asset(path: str, reason: str, *, size: int = 0) -> dict[str, Any]:
    return {
        "path": path,
        "bytes": max(size, 0),
        "status": "skipped",
        "reason": reason,
    }


def _file_size_or_zero(path: Path) -> int:
    try:
        return max(int(path.stat().st_size), 0)
    except OSError:
        return 0


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
    for tag, attr in (
        ("img", "src"),
        ("source", "src"),
        ("link", "href"),
        ("script", "src"),
        ("image", "href"),
        ("image", "xlink:href"),
        ("use", "href"),
        ("use", "xlink:href"),
    ):
        refs.extend(_html_attr_values(text, tag, attr))
    for tag in ("img", "source"):
        for srcset in _html_attr_values(text, tag, "srcset"):
            refs.extend(_srcset_urls(srcset))
    return refs


def _srcset_urls(value: str) -> list[str]:
    urls: list[str] = []
    for raw_entry in re.split(r",\s+", html.unescape(value).strip()):
        parts = raw_entry.strip().split()
        if parts:
            urls.append(parts[0])
    return urls


def _missing_local_refs(package_dir: Path, refs: list[str]) -> list[str]:
    missing: list[str] = []
    package_root = package_dir.resolve(strict=False)
    for value in refs:
        lowered = value.casefold()
        if not value or lowered.startswith(("http://", "https://", "//", "data:", "javascript:", "mailto:", "#")):
            continue
        if not _local_resource_exists(package_root, value):
            missing.append(value)
    return missing


def _local_resource_exists(package_root: Path, value: str) -> bool:
    raw_path = html.unescape(value).split("?", 1)[0].split("#", 1)[0]
    if not raw_path or len(raw_path) > 4096:
        return False
    path = Path(urllib.parse.unquote(raw_path))
    if path.is_absolute():
        return False
    try:
        candidate = (package_root / path).resolve(strict=True)
        candidate.relative_to(package_root)
        return candidate.is_file()
    except (OSError, ValueError):
        return False


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


def _web_polish_manifest(polished: Any) -> dict[str, Any]:
    kind = getattr(polished.kind, "value", str(polished.kind))
    return {
        "kind": str(kind),
        "article_extracted": bool(polished.article_extracted),
        "article_selector": polished.article_selector,
        "same_document_links_rewritten": int(polished.same_document_links_rewritten),
        "unresolved_same_document_links": int(polished.unresolved_same_document_links),
        "inlined_images": int(polished.inlined_images),
        "recovered_source_figures": int(polished.recovered_source_figures),
        "attempted_source_figures": int(polished.attempted_source_figures),
        "source_recovery_errors": list(polished.source_recovery_errors),
    }


def _unsafe_attribute_count(text: str) -> int:
    unsafe = re.compile(
        r"\son[A-Za-z][\w:-]*\s*=|\b(?:href|src|action|formaction|data)\s*=\s*"
        r"['\"]?\s*(?:javascript|vbscript|file):",
        re.IGNORECASE,
    )
    return sum(
        len(unsafe.findall(match.group(0)))
        for match in re.finditer(
            r"<[A-Za-z][^>]*>",
            text,
            re.DOTALL,
        )
    )


def _int_value(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_part(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return value.strip("._-")[:80] or "article"
