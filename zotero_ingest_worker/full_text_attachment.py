from __future__ import annotations

import base64
import mimetypes
import os
import re
import shutil
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from .full_text_article import (
    annotate_html_download_article_verdicts,
    html_download_article_verdict,
    is_arxiv_abs_landing_download,
)
from .article_standard import standardize_native_html_download
from .local_attachment_sync import sync_parent_attachment_local
from .local_zotero import LocalAttachment, LocalItemMetadata
from .source_html_maintenance import TrashAttachment, cleanup_source_html_inventory


CreateParentAttachment = Callable[..., dict[str, Any]]
EnqueueAttachedPdf = Callable[..., dict[str, Any]]
EnqueueAttachedHtml = Callable[..., dict[str, Any]]
HTML_ATTACHMENT_MAX_SOURCE_BYTES = 16_000_000
HTML_ATTACHMENT_MAX_ASSET_BYTES = 8_000_000
HTML_ATTACHMENT_MAX_TOTAL_ASSET_BYTES = 64_000_000
HTML_ATTACHMENT_MAX_ASSETS = 80
HTML_ATTACHMENT_MAX_SCANNED_ASSETS = 512


class FullTextAttachmentService:
    def __init__(
        self,
        *,
        relay_enabled: bool,
        create_parent_attachment: CreateParentAttachment,
        enqueue_pdf_for_ocr: EnqueueAttachedPdf,
        enqueue_pdf_for_html: EnqueueAttachedPdf,
        enqueue_html_for_translation: EnqueueAttachedHtml | None = None,
        trash_source_html_attachment: TrashAttachment | None = None,
        allow_raw_html_fallback: bool = False,
    ) -> None:
        self.relay_enabled = relay_enabled
        self.create_parent_attachment = create_parent_attachment
        self.enqueue_pdf_for_ocr = enqueue_pdf_for_ocr
        self.enqueue_pdf_for_html = enqueue_pdf_for_html
        self.enqueue_html_for_translation = enqueue_html_for_translation
        self.trash_source_html_attachment = trash_source_html_attachment
        self.allow_raw_html_fallback = allow_raw_html_fallback

    def attach(
        self,
        *,
        attachment: LocalAttachment,
        metadata: LocalItemMetadata,
        inventory: dict[str, object],
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not self.relay_enabled:
            return None

        html = _best_successful_html_download(payload.get("html_downloads"))
        pdf = _first_successful_download(payload.get("pdf_downloads"))
        html_result: dict[str, Any] | None = None
        if html is not None:
            html_result = self._attach_or_skip_html(
                html=html,
                attachment=attachment,
                metadata=metadata,
                inventory=inventory,
            )

        pdf_result: dict[str, Any] | None = None
        if pdf is not None:
            if inventory.get("has_pdf"):
                pdf_result = _skipped_existing_pdf_result(pdf=pdf, inventory=inventory)
            else:
                pdf_result = self._attach_pdf(
                    pdf=pdf,
                    attachment=attachment,
                    metadata=metadata,
                    inventory=inventory,
                )

        if html_result is not None and pdf_result is not None:
            if pdf_result.get("skipped"):
                return html_result
            if html_result.get("skipped") and pdf_result.get("ok"):
                pdf_result = dict(pdf_result)
                pdf_result["html_attachment"] = html_result
                return pdf_result
            if html_result.get("ok") and pdf_result.get("ok"):
                return _with_pdf_attachment_result(html_result=html_result, pdf_result=pdf_result)
            if not html_result.get("ok") and pdf_result.get("ok"):
                pdf_result = dict(pdf_result)
                pdf_result["html_attachment"] = html_result
                return pdf_result
            html_result = dict(html_result)
            html_result["pdf_attachment"] = pdf_result
            return html_result
        if html_result is not None:
            return html_result
        return pdf_result

    def _attach_or_skip_html(
        self,
        *,
        html: dict[str, Any],
        attachment: LocalAttachment,
        metadata: LocalItemMetadata,
        inventory: dict[str, object],
    ) -> dict[str, Any]:
        source_path = _html_attachment_first_existing_source_path(html)
        if source_path is None:
            return {
                "ok": False,
                "status": "local_source_missing",
                "sourcePath": str(_html_attachment_preferred_source_path(html)),
            }
        cleanup = cleanup_source_html_inventory(
            metadata=metadata,
            inventory=inventory,
            storage_dir=attachment.storage_dir,
            trash_attachment=self.trash_source_html_attachment,
            dry_run=False,
        )
        if not cleanup.get("ok"):
            return {
                "ok": False,
                "kind": "html",
                "status": "source_html_cleanup_failed",
                "source": html,
                "source_html_cleanup": cleanup,
            }
        keep_key = str(cleanup.get("keep_key") or "").strip()
        if keep_key:
            return _skipped_existing_html_result(
                html=html,
                inventory=inventory,
                keep_key=keep_key,
                cleanup=cleanup,
            )
        result = self._attach_html(
            html=html,
            attachment=attachment,
            metadata=metadata,
            inventory=inventory,
        )
        result["source_html_cleanup"] = cleanup
        return result

    def _attach_html(
        self,
        *,
        html: dict[str, Any],
        attachment: LocalAttachment,
        metadata: LocalItemMetadata,
        inventory: dict[str, object],
    ) -> dict[str, Any]:
        prepared = self._prepare_html_attachment_source(html=html, metadata=metadata)
        if not prepared.get("ok"):
            return prepared
        source_path = Path(str(prepared["source_path"]))
        attachment_source_path, embedded_assets = _html_attachment_source_with_embedded_assets(source_path)
        filename = _full_text_attachment_filename(
            source_path=source_path,
            title=metadata.title,
            suffix="SOURCE HTML",
            extension=".html",
        )
        relay_result = self.create_parent_attachment(
            metadata=metadata,
            attachment=attachment,
            source_path=attachment_source_path,
            filename=filename,
            title=f"{metadata.title or filename} [source HTML]",
            content_type="text/html",
            probe_attachment_key=_inventory_probe_attachment_key(inventory),
            dedupe_prefix="full-text-html",
        )
        try:
            local_copy = write_parent_attachment_local_copy(
                attachment=attachment,
                source_path=attachment_source_path,
                filename=filename,
                relay_result=relay_result,
            )
        except Exception as exc:
            local_copy = _local_failure("local_copy_failed", exc)
        if local_copy.get("ok"):
            try:
                local_metadata = sync_parent_attachment_local(
                    metadata=metadata,
                    attachment=attachment,
                    filename=filename,
                    title=f"{metadata.title or filename} [source HTML]",
                    content_type="text/html",
                    relay_result=relay_result,
                )
            except Exception as exc:
                local_metadata = _local_failure("local_metadata_failed", exc)
        else:
            local_metadata = {"ok": False, "skipped": True, "reason": "local_copy_failed"}
        result: dict[str, Any] = {
            "ok": True,
            "kind": "html",
            "source": html,
            "raw_source_path": prepared.get("raw_source_path"),
            "article_standard": prepared.get("article_standard"),
            "raw_html_fallback": bool(prepared.get("raw_html_fallback")),
            "attachment_source_path": str(attachment_source_path),
            "embedded_assets": embedded_assets,
            "relay": relay_result,
            "local_copy": local_copy,
            "local_metadata": local_metadata,
        }
        if local_copy.get("ok") and self.enqueue_html_for_translation is not None:
            try:
                result["translation_enqueue"] = self.enqueue_html_for_translation(
                    metadata=metadata,
                    attachment=attachment,
                    source_path=Path(str(local_copy["path"])),
                    relay_result=relay_result,
                )
            except Exception as exc:
                result["translation_enqueue"] = _local_failure("translation_enqueue_failed", exc)
        return result

    def _prepare_html_attachment_source(
        self,
        *,
        html: dict[str, Any],
        metadata: LocalItemMetadata,
    ) -> dict[str, Any]:
        standard_path = _html_attachment_existing_standard_path(html)
        raw_source_path = _html_attachment_raw_source_path(html)
        if standard_path is not None:
            return {
                "ok": True,
                "source_path": standard_path,
                "raw_source_path": str(raw_source_path),
                "article_standard": _html_attachment_standard_package(html),
                "raw_html_fallback": False,
            }
        if not raw_source_path.exists():
            return {
                "ok": False,
                "kind": "html",
                "status": "local_source_missing",
                "sourcePath": str(raw_source_path),
                "source": html,
            }

        download = dict(html)
        verdict = download.get("article_verdict")
        if not isinstance(verdict, dict):
            verdict = html_download_article_verdict(download)
            download["article_verdict"] = verdict
            html["article_verdict"] = verdict
        package = standardize_native_html_download(
            download,
            metadata=metadata,
            package_root=raw_source_path.parent / "article_packages",
            source_context="full_text_attachment",
        )
        html["standard_package"] = package
        article_html_path = Path(str(package.get("article_html_path") or ""))
        if package.get("ok") and article_html_path.exists():
            html["standard_article_html_path"] = str(article_html_path)
            return {
                "ok": True,
                "source_path": article_html_path,
                "raw_source_path": str(raw_source_path),
                "article_standard": package,
                "raw_html_fallback": False,
            }
        if self.allow_raw_html_fallback:
            return {
                "ok": True,
                "source_path": raw_source_path,
                "raw_source_path": str(raw_source_path),
                "article_standard": package,
                "raw_html_fallback": True,
            }
        return {
            "ok": False,
            "kind": "html",
            "status": "source_html_polish_failed",
            "sourcePath": str(raw_source_path),
            "source": html,
            "article_standard": package,
        }

    def _attach_pdf(
        self,
        *,
        pdf: dict[str, Any],
        attachment: LocalAttachment,
        metadata: LocalItemMetadata,
        inventory: dict[str, object],
    ) -> dict[str, Any]:
        source_path = Path(str(pdf.get("output_path") or ""))
        if not source_path.exists():
            return {"ok": False, "status": "local_source_missing", "sourcePath": str(source_path)}
        filename = _full_text_attachment_filename(
            source_path=source_path,
            title=metadata.title,
            suffix="FULL TEXT",
            extension=".pdf",
        )
        relay_result = self.create_parent_attachment(
            metadata=metadata,
            attachment=attachment,
            source_path=source_path,
            filename=filename,
            title=f"{metadata.title or filename} [full text]",
            content_type="application/pdf",
            probe_attachment_key=_inventory_probe_attachment_key(inventory),
            dedupe_prefix="full-text-pdf",
        )
        try:
            local_copy = write_parent_attachment_local_copy(
                attachment=attachment,
                source_path=source_path,
                filename=filename,
                relay_result=relay_result,
            )
        except Exception as exc:
            local_copy = _local_failure("local_copy_failed", exc)
        if local_copy.get("ok"):
            try:
                local_metadata = sync_parent_attachment_local(
                    metadata=metadata,
                    attachment=attachment,
                    filename=filename,
                    title=f"{metadata.title or filename} [full text]",
                    content_type="application/pdf",
                    relay_result=relay_result,
                )
            except Exception as exc:
                local_metadata = _local_failure("local_metadata_failed", exc)
        else:
            local_metadata = {"ok": False, "skipped": True, "reason": "local_copy_failed"}
        result: dict[str, Any] = {
            "ok": True,
            "kind": "pdf",
            "source": pdf,
            "relay": relay_result,
            "local_copy": local_copy,
            "local_metadata": local_metadata,
        }
        if not local_copy.get("ok"):
            result["pdf_enqueue"] = {
                "ok": False,
                "skipped": True,
                "reason": "local_copy_failed",
            }
            return result
        if _download_needs_ocr(pdf):
            result["ocr_enqueue"] = self.enqueue_pdf_for_ocr(
                metadata=metadata,
                attachment=attachment,
                source_path=Path(str(local_copy["path"])),
                relay_result=relay_result,
            )
        else:
            result["html_enqueue"] = self.enqueue_pdf_for_html(
                metadata=metadata,
                attachment=attachment,
                source_path=Path(str(local_copy["path"])),
                relay_result=relay_result,
            )
        return result


def _local_failure(reason: str, exc: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "reason": reason,
        "error": f"{type(exc).__name__}: {exc}",
    }


def _skipped_existing_pdf_result(
    *,
    pdf: dict[str, Any],
    inventory: dict[str, object],
) -> dict[str, Any]:
    return {
        "ok": True,
        "skipped": True,
        "kind": "pdf",
        "reason": "parent_already_has_pdf",
        "has_html": bool(inventory.get("has_html")),
        "has_pdf": bool(inventory.get("has_pdf")),
        "source": pdf,
    }


def _skipped_existing_html_result(
    *,
    html: dict[str, Any],
    inventory: dict[str, object],
    keep_key: str,
    cleanup: dict[str, Any],
) -> dict[str, Any]:
    return {
        "ok": True,
        "skipped": True,
        "kind": "html",
        "reason": "parent_already_has_source_html",
        "existing_attachment_key": keep_key,
        "has_html": bool(inventory.get("has_html")),
        "has_source_html": bool(inventory.get("has_source_html")),
        "source": html,
        "source_html_cleanup": cleanup,
    }


def _with_pdf_attachment_result(
    *,
    html_result: dict[str, Any],
    pdf_result: dict[str, Any],
) -> dict[str, Any]:
    result = dict(html_result)
    result["ok"] = bool(html_result.get("ok")) and bool(pdf_result.get("ok"))
    result["attached_kinds"] = ["html", "pdf"]
    result["attachments"] = [html_result, pdf_result]
    result["html_attachment"] = html_result
    result["pdf_attachment"] = pdf_result
    if "relay" in pdf_result:
        result["pdf_relay"] = pdf_result["relay"]
    if "local_copy" in pdf_result:
        result["pdf_local_copy"] = pdf_result["local_copy"]
    return result


def write_parent_attachment_local_copy(
    *,
    attachment: LocalAttachment,
    source_path: Path,
    filename: str,
    relay_result: dict[str, Any],
) -> dict[str, Any]:
    new_key = _relay_attachment_key(relay_result)
    if not new_key:
        raise RuntimeError("zotero-file-relay parent attachment did not return an attachment key.")
    target_dir = attachment.storage_dir / new_key
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / Path(filename).name
    temp_path = target_dir / f".{target_path.name}.full-text-tmp"
    shutil.copy2(source_path, temp_path)
    os.replace(temp_path, target_path)
    return {"ok": True, "attachmentKey": new_key, "path": str(target_path)}


def local_attachment_from_relay(
    *,
    metadata: LocalItemMetadata,
    attachment: LocalAttachment,
    source_path: Path,
    relay_result: dict[str, Any],
    content_type: str,
) -> LocalAttachment:
    new_key = _relay_attachment_key(relay_result)
    if not new_key:
        raise RuntimeError("Relay result did not include a new attachment key.")
    return LocalAttachment(
        library_id=metadata.library_id,
        data_dir=metadata.data_dir,
        storage_dir=attachment.storage_dir,
        key=new_key,
        item_id=None,
        parent_item_id=metadata.item_id,
        date_modified=None,
        link_mode=None,
        content_type=content_type,
        zotero_path=f"storage:{source_path.name}",
        file_path=source_path,
        parent_key=metadata.key,
    )


def _first_successful_download(value: object) -> dict[str, Any] | None:
    if not isinstance(value, list):
        return None
    for item in value:
        if not isinstance(item, dict):
            continue
        if item.get("ok") and str(item.get("output_path") or "").strip():
            return item
    return None


def _html_attachment_preferred_source_path(item: dict[str, Any]) -> Path:
    standard_path = str(item.get("standard_article_html_path") or "").strip()
    if standard_path:
        return Path(standard_path)
    standard_package = item.get("standard_package")
    if isinstance(standard_package, dict):
        package_path = str(standard_package.get("article_html_path") or "").strip()
        if package_path:
            return Path(package_path)
    return Path(str(item.get("output_path") or ""))


def _html_attachment_raw_source_path(item: dict[str, Any]) -> Path:
    return Path(str(item.get("output_path") or ""))


def _html_attachment_standard_package(item: dict[str, Any]) -> dict[str, Any] | None:
    package = item.get("standard_package")
    return package if isinstance(package, dict) else None


def _html_attachment_existing_standard_path(item: dict[str, Any]) -> Path | None:
    for candidate in (
        str(item.get("standard_article_html_path") or "").strip(),
        str((_html_attachment_standard_package(item) or {}).get("article_html_path") or "").strip(),
    ):
        if not candidate:
            continue
        path = Path(candidate)
        if path.exists():
            return path
    return None


def _html_attachment_first_existing_source_path(item: dict[str, Any]) -> Path | None:
    standard_path = _html_attachment_existing_standard_path(item)
    if standard_path is not None:
        return standard_path
    raw_source_path = _html_attachment_raw_source_path(item)
    if raw_source_path.exists():
        return raw_source_path
    return None


def _best_successful_html_download(value: object) -> dict[str, Any] | None:
    if not isinstance(value, list):
        return None
    annotate_html_download_article_verdicts(value)
    candidates: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        verdict = item.get("article_verdict")
        if not isinstance(verdict, dict):
            verdict = html_download_article_verdict(item)
            item["article_verdict"] = verdict
        if item.get("ok") and str(item.get("output_path") or "").strip() and verdict.get("ok"):
            candidates.append(item)
    if not candidates:
        return None
    return max(candidates, key=_html_download_score)


def _html_download_score(item: dict[str, Any]) -> tuple[int, int, int, int, int]:
    article = item.get("article")
    article_data = article if isinstance(article, dict) else {}
    markers = {
        str(marker).casefold()
        for marker in article_data.get("markers", [])
        if str(marker).strip()
    }
    section_markers = {
        str(marker).casefold()
        for marker in article_data.get("section_markers", [])
        if str(marker).strip()
    }
    url = f"{item.get('url') or ''} {item.get('final_url') or ''}".casefold()
    kind = str(item.get("kind") or "").casefold()
    source = str(item.get("source") or "").casefold()
    try:
        text_chars = int(article_data.get("text_chars") or 0)
    except (TypeError, ValueError):
        text_chars = 0

    full_article_bonus = 0
    if kind == "html":
        full_article_bonus += 2
    if "/html/" in url:
        full_article_bonus += 2
    if source == "arxiv":
        full_article_bonus += 1
    if markers.intersection({"article_tag", "arxiv_ltx_document", "arxiv_ltx_bibliography"}):
        full_article_bonus += 2
    if section_markers.intersection({"methods", "results", "discussion", "conclusion"}):
        full_article_bonus += 1

    landing_penalty = 1 if kind == "landing" else 0
    references_bonus = 1 if "references" in markers or "references" in section_markers else 0
    return (full_article_bonus, references_bonus, text_chars, -landing_penalty, -len(str(item.get("output_path") or "")))


_is_arxiv_abs_landing_download = is_arxiv_abs_landing_download


def _html_attachment_source_with_embedded_assets(
    source_path: Path,
    *,
    max_source_bytes: int = HTML_ATTACHMENT_MAX_SOURCE_BYTES,
    max_asset_bytes: int = HTML_ATTACHMENT_MAX_ASSET_BYTES,
    max_total_asset_bytes: int = HTML_ATTACHMENT_MAX_TOTAL_ASSET_BYTES,
    max_assets: int = HTML_ATTACHMENT_MAX_ASSETS,
    max_scanned_assets: int = HTML_ATTACHMENT_MAX_SCANNED_ASSETS,
) -> tuple[Path, dict[str, Any]]:
    if source_path.suffix.casefold() not in {".html", ".htm", ".xhtml"}:
        return source_path, {"enabled": False, "reason": "not_html"}

    assets_dir = source_path.parent / f"{source_path.stem}_assets"
    if not assets_dir.is_dir() and source_path.name == "article.html":
        standard_assets_dir = source_path.parent / "assets"
        if standard_assets_dir.is_dir():
            assets_dir = standard_assets_dir
    if not assets_dir.is_dir():
        return source_path, {"enabled": False, "reason": "assets_dir_missing"}

    asset_files, scan_truncated = _local_asset_candidates(
        assets_dir,
        max_scanned_assets=max_scanned_assets,
    )
    if not asset_files:
        return source_path, {"enabled": False, "reason": "assets_empty", "assets_dir": str(assets_dir)}

    source_payload = _read_file_bounded(source_path, max_bytes=max_source_bytes)
    if source_payload is None:
        return source_path, {
            "enabled": False,
            "reason": "source_too_large",
            "assets_dir": str(assets_dir),
            "source_bytes": _file_size_or_zero(source_path),
            "max_source_bytes": max(max_source_bytes, 0),
        }

    html_text = source_payload.decode("utf-8", errors="replace")
    budget = _LocalAssetEmbeddingBudget(
        assets_dir=assets_dir,
        max_asset_bytes=max_asset_bytes,
        max_total_bytes=max_total_asset_bytes,
        max_assets=max_assets,
    )
    css_text_by_rel: dict[str, str] = {}
    data_uri_by_rel: dict[str, str] = {}
    missing_local_refs: list[str] = []

    for asset in asset_files:
        if _is_css_asset(asset):
            continue
        if budget.relative_path(asset) is None:
            continue
        rel = _asset_html_relpath(assets_dir, asset)
        variants = _asset_reference_variants(rel)
        if not any(variant in html_text for variant in variants):
            continue
        data_uri = budget.data_uri(asset)
        if data_uri is not None:
            data_uri_by_rel[rel] = data_uri

    for asset in asset_files:
        if not _is_css_asset(asset):
            continue
        if budget.relative_path(asset) is None:
            continue
        rel = _asset_html_relpath(assets_dir, asset)
        variants = _asset_reference_variants(rel)
        if not any(variant in html_text for variant in variants):
            continue
        css_text, css_missing = _css_with_embedded_local_assets(
            asset,
            assets_dir=assets_dir,
            budget=budget,
        )
        missing_local_refs.extend(css_missing)
        if css_text is not None:
            css_text_by_rel[rel] = css_text

    if scan_truncated:
        budget.record_skip(assets_dir, "asset_scan_limit")
    if not data_uri_by_rel and not css_text_by_rel:
        return source_path, {
            "enabled": False,
            "reason": "no_embeddable_assets",
            "assets_dir": str(assets_dir),
            "asset_count": len(asset_files),
            "skipped_asset_count": budget.skipped_count,
            "skipped_assets": budget.skipped[:20],
        }

    rewritten = _replace_stylesheet_links_with_style_tags(html_text, css_text_by_rel)
    rewritten = _replace_style_imports_with_embedded_css(rewritten, css_text_by_rel)
    for rel, data_uri in sorted(data_uri_by_rel.items(), key=lambda item: len(item[0]), reverse=True):
        for variant in _asset_reference_variants(rel):
            rewritten = rewritten.replace(variant, data_uri)

    embedded_path = source_path.with_name(f"{source_path.stem}.z2m_embedded.html")
    embedded_path.write_text(rewritten, encoding="utf-8")
    return embedded_path, {
        "enabled": True,
        "source_path": str(source_path),
        "output_path": str(embedded_path),
        "assets_dir": str(assets_dir),
        "asset_count": len(asset_files),
        "embedded_assets": budget.embedded_asset_count,
        "embedded_stylesheets": budget.embedded_stylesheet_count,
        "embedded_source_bytes": budget.total_bytes,
        "missing_local_refs": missing_local_refs[:20],
        "skipped_asset_count": budget.skipped_count,
        "skipped_assets": budget.skipped[:20],
    }


def _replace_stylesheet_links_with_style_tags(html_text: str, css_text_by_rel: dict[str, str]) -> str:
    if not css_text_by_rel:
        return html_text

    pattern = re.compile(
        r"<link\b(?=[^>]*\brel=[\"'][^\"']*stylesheet[^\"']*[\"'])(?=[^>]*\bhref=[\"']([^\"']+)[\"'])[^>]*>",
        flags=re.IGNORECASE,
    )

    def replace(match: re.Match[str]) -> str:
        href = match.group(1)
        css_text = css_text_by_rel.get(href) or css_text_by_rel.get(urllib.parse.unquote(href))
        if css_text is None:
            return match.group(0)
        safe_css = _css_text_for_style_tag(css_text)
        return f"<style>\n{safe_css}\n</style>"

    return pattern.sub(replace, html_text)


def _replace_style_imports_with_embedded_css(html_text: str, css_text_by_rel: dict[str, str]) -> str:
    if not css_text_by_rel:
        return html_text

    def replace(match: re.Match[str]) -> str:
        raw_url = match.group("url").strip()
        css_text = _css_text_for_reference(raw_url, css_text_by_rel)
        if css_text is None:
            return match.group(0)
        return _css_import_replacement(css_text, match.group("tail") or "")

    return _css_import_pattern().sub(replace, html_text)


def _css_with_embedded_local_assets(
    css_path: Path,
    *,
    assets_dir: Path,
    budget: _LocalAssetEmbeddingBudget,
    seen: set[Path] | None = None,
) -> tuple[str | None, list[str]]:
    seen = seen or set()
    resolved_css_path = budget.resolved_path(css_path)
    if resolved_css_path is None:
        return None, [str(css_path)]
    if resolved_css_path in seen:
        return None, [str(css_path)]
    seen.add(resolved_css_path)
    css_text = budget.css_text(css_path)
    if css_text is None:
        seen.discard(resolved_css_path)
        return None, [str(css_path)]
    missing: list[str] = []

    def replace_import(match: re.Match[str]) -> str:
        raw_url = match.group("url").strip()
        target = _local_asset_target(css_path.parent, raw_url, assets_dir=assets_dir)
        if target is None:
            return match.group(0)
        if not target.is_file():
            missing.append(raw_url)
            return match.group(0)
        imported_css, imported_missing = _css_with_embedded_local_assets(
            target,
            assets_dir=assets_dir,
            budget=budget,
            seen=seen,
        )
        missing.extend(imported_missing)
        if imported_css is None:
            return match.group(0)
        return _css_import_replacement(imported_css, match.group("tail") or "")

    def replace(match: re.Match[str]) -> str:
        raw_url = match.group(1).strip().strip("\"'")
        if not raw_url or _is_external_or_data_url(raw_url):
            return match.group(0)
        target = _local_asset_target(css_path.parent, raw_url, assets_dir=assets_dir)
        if target is None:
            return match.group(0)
        if not target.is_file():
            missing.append(raw_url)
            return match.group(0)
        data_uri = budget.data_uri(target)
        if data_uri is None:
            missing.append(raw_url)
            return match.group(0)
        return f'url("{data_uri}")'

    css_text = _css_import_pattern().sub(replace_import, css_text)
    css_text = re.sub(r"url\(([^)]+)\)", replace, css_text, flags=re.IGNORECASE)
    seen.discard(resolved_css_path)
    return css_text, missing


def _css_import_pattern() -> re.Pattern[str]:
    return re.compile(
        r"@import\s+(?:url\(\s*)?[\"']?(?P<url>[^\"')\s;]+)[\"']?\s*\)?(?P<tail>[^;]*);",
        flags=re.IGNORECASE,
    )


def _css_import_replacement(css_text: str, tail: str) -> str:
    css_text = _css_text_for_style_tag(css_text)
    layer = re.search(r"\blayer\s*(?:\(\s*([^)]+?)\s*\))?", tail or "", flags=re.IGNORECASE)
    if not layer:
        return css_text
    layer_name = (layer.group(1) or "").strip()
    if not layer_name:
        return f"@layer {{\n{css_text}\n}}"
    return f"@layer {layer_name} {{\n{css_text}\n}}"


def _css_text_for_style_tag(css_text: str) -> str:
    css_text = re.sub(r"^\s*@charset\s+[^;]+;\s*", "", css_text, flags=re.IGNORECASE)
    return css_text.replace("</style", "<\\/style")


def _css_text_for_reference(raw_url: str, css_text_by_rel: dict[str, str]) -> str | None:
    variants = _asset_reference_variants(raw_url)
    if raw_url.startswith("./"):
        variants.extend(_asset_reference_variants(raw_url[2:]))
    unquoted = urllib.parse.unquote(raw_url)
    variants.extend(_asset_reference_variants(unquoted))
    if unquoted.startswith("./"):
        variants.extend(_asset_reference_variants(unquoted[2:]))
    for variant in dict.fromkeys(variants):
        css_text = css_text_by_rel.get(variant)
        if css_text is not None:
            return css_text
    return None


def _local_asset_target(base_dir: Path, raw_url: str, *, assets_dir: Path) -> Path | None:
    parsed = urllib.parse.urlparse(raw_url)
    local_path = urllib.parse.unquote(parsed.path)
    if not local_path:
        return None
    target = (base_dir / local_path).resolve()
    try:
        target.relative_to(assets_dir.resolve())
    except ValueError:
        return None
    return target


def _asset_html_relpath(assets_dir: Path, asset: Path) -> str:
    return f"{assets_dir.name}/{asset.relative_to(assets_dir).as_posix()}"


def _asset_reference_variants(rel: str) -> list[str]:
    quoted = urllib.parse.quote(rel, safe="/._-")
    variants = [rel, quoted]
    return list(dict.fromkeys(variants))


def _local_asset_candidates(
    assets_dir: Path,
    *,
    max_scanned_assets: int,
) -> tuple[list[Path], bool]:
    limit = max(max_scanned_assets, 0)
    files: list[Path] = []
    truncated = False
    for path in assets_dir.rglob("*"):
        if not path.is_file() and not path.is_symlink():
            continue
        if len(files) >= limit:
            truncated = True
            break
        files.append(path)
    files.sort(key=lambda path: str(path).casefold())
    return files, truncated


def _read_file_bounded(path: Path, *, max_bytes: int) -> bytes | None:
    limit = max(max_bytes, 0)
    with path.open("rb") as stream:
        payload = stream.read(limit + 1)
    if len(payload) > limit:
        return None
    return payload


def _file_size_or_zero(path: Path) -> int:
    try:
        return max(int(path.stat().st_size), 0)
    except OSError:
        return 0


class _LocalAssetEmbeddingBudget:
    def __init__(
        self,
        *,
        assets_dir: Path,
        max_asset_bytes: int,
        max_total_bytes: int,
        max_assets: int,
    ) -> None:
        self.assets_dir = assets_dir
        self.root = assets_dir.resolve()
        self.max_asset_bytes = max(max_asset_bytes, 0)
        self.max_total_bytes = max(max_total_bytes, 0)
        self.max_assets = max(max_assets, 0)
        self.total_bytes = 0
        self.skipped: list[dict[str, str]] = []
        self._skipped_keys: set[tuple[str, str]] = set()
        self._reserved: dict[Path, int] = {}
        self._data_uris: dict[Path, str] = {}
        self._css_text: dict[Path, str] = {}

    @property
    def embedded_asset_count(self) -> int:
        return len(self._data_uris)

    @property
    def embedded_stylesheet_count(self) -> int:
        return len(self._css_text)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)

    def record_skip(self, path: Path, reason: str) -> None:
        key = (str(path), reason)
        if key in self._skipped_keys:
            return
        self._skipped_keys.add(key)
        self.skipped.append({"path": str(path), "reason": reason})

    def resolved_path(self, path: Path) -> Path | None:
        if path.is_symlink():
            self.record_skip(path, "asset_symlink")
            return None
        try:
            lexical_relative = path.relative_to(self.assets_dir)
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.root)
        except (OSError, ValueError):
            self.record_skip(path, "asset_outside_root")
            return None
        if not lexical_relative.parts or not resolved.is_file():
            self.record_skip(path, "asset_not_file")
            return None
        return resolved

    def relative_path(self, path: Path) -> Path | None:
        if self.resolved_path(path) is None:
            return None
        return path.relative_to(self.assets_dir)

    def data_uri(self, path: Path) -> str | None:
        resolved = self.resolved_path(path)
        if resolved is None:
            return None
        cached = self._data_uris.get(resolved)
        if cached is not None:
            return cached
        payload = self._reserve_and_read(path, resolved)
        if payload is None:
            return None
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data_uri = _data_uri(payload, mime_type)
        self._data_uris[resolved] = data_uri
        return data_uri

    def css_text(self, path: Path) -> str | None:
        resolved = self.resolved_path(path)
        if resolved is None:
            return None
        cached = self._css_text.get(resolved)
        if cached is not None:
            return cached
        payload = self._reserve_and_read(path, resolved)
        if payload is None:
            return None
        css_text = payload.decode("utf-8", errors="replace")
        self._css_text[resolved] = css_text
        return css_text

    def _reserve_and_read(self, path: Path, resolved: Path) -> bytes | None:
        if resolved in self._reserved:
            self.record_skip(path, "asset_type_conflict")
            return None
        if len(self._reserved) >= self.max_assets:
            self.record_skip(path, "asset_count_limit")
            return None
        size = _file_size_or_zero(resolved)
        if size > self.max_asset_bytes:
            self.record_skip(path, "asset_too_large")
            return None
        if self.total_bytes + size > self.max_total_bytes:
            self.record_skip(path, "asset_total_bytes_limit")
            return None
        payload = _read_file_bounded(resolved, max_bytes=self.max_asset_bytes)
        if payload is None:
            self.record_skip(path, "asset_too_large")
            return None
        if self.total_bytes + len(payload) > self.max_total_bytes:
            self.record_skip(path, "asset_total_bytes_limit")
            return None
        self._reserved[resolved] = len(payload)
        self.total_bytes += len(payload)
        return payload


def _data_uri(payload: bytes, mime_type: str) -> str:
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _is_css_asset(path: Path) -> bool:
    return path.suffix.casefold() == ".css"


def _is_external_or_data_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return bool(parsed.scheme or parsed.netloc) or url.strip().casefold().startswith(("data:", "#"))


def _download_needs_ocr(item: dict[str, Any]) -> bool:
    identity = item.get("identity")
    if item.get("status") == "downloaded_needs_ocr":
        return True
    return isinstance(identity, dict) and bool(identity.get("needs_ocr"))


def _inventory_probe_attachment_key(inventory: dict[str, object]) -> str | None:
    attachments = inventory.get("attachments")
    if not isinstance(attachments, list):
        return None
    for wanted in ("application/pdf", "text/html", "application/xhtml+xml", "multipart/related", "message/rfc822"):
        for item in attachments:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            content_type = str(item.get("content_type") or "").casefold()
            if key and content_type == wanted:
                return key
    for item in attachments:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if key:
            return key
    return None


def _full_text_attachment_filename(
    *,
    source_path: Path,
    title: str,
    suffix: str,
    extension: str,
) -> str:
    stem = _safe_filename(title or source_path.stem)
    return f"{stem} [{suffix}]{extension}"


def _relay_attachment_key(relay_result: dict[str, Any]) -> str:
    return str(
        relay_result.get("newAttachmentKey")
        or relay_result.get("attachmentKey")
        or relay_result.get("siblingKey")
        or ""
    ).strip()


def _safe_filename(value: str) -> str:
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", str(value or "document"))
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:180] or "document"
