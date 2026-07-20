from __future__ import annotations

import base64
import html
import mimetypes
import os
import re
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .full_text_article import (
    annotate_html_download_article_verdicts,
    html_download_article_verdict,
    is_arxiv_abs_landing_download,
)
from .article_standard import (
    _FileFingerprint,
    _copy_file_bounded,
    _copy_file_bounded_with_owner,
    _read_file_snapshot_bounded,
    _same_file_content,
    _stable_file_fingerprint,
    _unlink_owned_regular_file,
    _write_text_file_bounded,
    standardize_native_html_download,
    validated_article_package_html_path,
)
from .filename_safety import safe_filename_component
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
HTML_ATTACHMENT_MAX_OUTPUT_BYTES = 128_000_000
HTML_ATTACHMENT_PUBLICATION_VERIFY_ATTEMPTS = 32
HTML_ATTACHMENT_PUBLICATION_VERIFY_DELAY_SECONDS = 0.002


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
        ensure_active: Callable[[], None] | None = None,
    ) -> None:
        self.relay_enabled = relay_enabled
        self.create_parent_attachment = create_parent_attachment
        self.enqueue_pdf_for_ocr = enqueue_pdf_for_ocr
        self.enqueue_pdf_for_html = enqueue_pdf_for_html
        self.enqueue_html_for_translation = enqueue_html_for_translation
        self.trash_source_html_attachment = trash_source_html_attachment
        self.allow_raw_html_fallback = allow_raw_html_fallback
        self.ensure_active = ensure_active

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
        self._ensure_active()

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
            if inventory.get("has_pdf") is True:
                pdf_result = _skipped_existing_pdf_result(pdf=pdf, inventory=inventory)
            else:
                pdf_result = self._attach_pdf(
                    pdf=pdf,
                    attachment=attachment,
                    metadata=metadata,
                    inventory=inventory,
                )

        if html_result is not None and pdf_result is not None:
            if pdf_result.get("skipped") is True:
                return html_result
            if html_result.get("skipped") is True and pdf_result.get("ok") is True:
                pdf_result = dict(pdf_result)
                pdf_result["html_attachment"] = html_result
                return pdf_result
            if html_result.get("ok") is True and pdf_result.get("ok") is True:
                return _with_pdf_attachment_result(
                    html_result=html_result, pdf_result=pdf_result
                )
            if html_result.get("ok") is not True and pdf_result.get("ok") is True:
                pdf_result = dict(pdf_result)
                pdf_result["html_attachment"] = html_result
                return pdf_result
            html_result = dict(html_result)
            html_result["pdf_attachment"] = pdf_result
            return html_result
        if html_result is not None:
            return html_result
        return pdf_result

    def _ensure_active(self) -> None:
        if self.ensure_active is not None:
            self.ensure_active()

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
        cleanup_value = cleanup_source_html_inventory(
            metadata=metadata,
            inventory=inventory,
            storage_dir=attachment.storage_dir,
            trash_attachment=self.trash_source_html_attachment,
            dry_run=False,
            ensure_active=self._ensure_active,
        )
        if not isinstance(cleanup_value, dict):
            cleanup = _downstream_result(
                cleanup_value,
                failure_reason="source_html_cleanup_invalid_result",
            )
            return {
                "ok": False,
                "kind": "html",
                "status": "source_html_cleanup_invalid_result",
                "source": html,
                "source_html_cleanup": cleanup,
            }
        cleanup = cleanup_value
        if cleanup.get("ok") is not True:
            return {
                "ok": False,
                "kind": "html",
                "status": "source_html_cleanup_failed",
                "source": html,
                "source_html_cleanup": cleanup,
            }
        keep_key_value = cleanup.get("keep_key")
        keep_key = _validated_zotero_attachment_key(keep_key_value)
        if keep_key_value is not None and (
            not isinstance(keep_key_value, str)
            or (keep_key_value.strip() and not keep_key)
        ):
            return {
                "ok": False,
                "kind": "html",
                "status": "source_html_cleanup_invalid_result",
                "source": html,
                "source_html_cleanup": cleanup,
            }
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
        if prepared.get("ok") is not True:
            return prepared
        source_path = Path(str(prepared["source_path"]))
        attachment_source_path, embedded_assets = (
            _html_attachment_source_with_embedded_assets(source_path)
        )
        if embedded_assets.get("failed") is True:
            return {
                "ok": False,
                "kind": "html",
                "status": "html_embedding_failed",
                "source": html,
                "raw_source_path": prepared.get("raw_source_path"),
                "article_standard": prepared.get("article_standard"),
                "embedded_assets": embedded_assets,
            }
        filename = _full_text_attachment_filename(
            source_path=source_path,
            title=metadata.title,
            suffix="SOURCE HTML",
            extension=".html",
        )
        try:
            expected_source = _stable_file_fingerprint(
                attachment_source_path,
                max_bytes=HTML_ATTACHMENT_MAX_OUTPUT_BYTES,
            )
        except OSError as exc:
            return {
                "ok": False,
                "kind": "html",
                "status": "attachment_source_unstable_before_relay",
                "source": html,
                "attachment_source_path": str(attachment_source_path),
                "embedded_assets": embedded_assets,
                "error": f"{type(exc).__name__}: {exc}"[:500],
            }
        output_record = embedded_assets.get("output")
        embedded_output_missing = embedded_assets.get(
            "enabled"
        ) is True and not isinstance(
            output_record,
            dict,
        )
        embedded_output_mismatch = isinstance(output_record, dict) and (
            output_record.get("bytes") != expected_source.bytes
            or output_record.get("sha256") != expected_source.sha256
        )
        if embedded_output_missing or embedded_output_mismatch:
            return {
                "ok": False,
                "kind": "html",
                "status": "embedded_output_integrity_mismatch",
                "source": html,
                "attachment_source_path": str(attachment_source_path),
                "embedded_assets": embedded_assets,
            }
        try:
            snapshot = _create_parent_attachment_source_snapshot(
                attachment_source_path,
                expected_source=expected_source,
                max_bytes=HTML_ATTACHMENT_MAX_OUTPUT_BYTES,
            )
        except OSError as exc:
            return {
                "ok": False,
                "kind": "html",
                "status": "attachment_snapshot_failed",
                "source": html,
                "raw_source_path": prepared.get("raw_source_path"),
                "article_standard": prepared.get("article_standard"),
                "attachment_source_path": str(attachment_source_path),
                "embedded_assets": embedded_assets,
                "error": f"{type(exc).__name__}: {exc}"[:500],
            }
        snapshot_source = snapshot.fingerprint
        try:
            self._ensure_active()
        except BaseException:
            snapshot.close()
            raise
        try:
            relay_result = _downstream_result(
                self.create_parent_attachment(
                    metadata=metadata,
                    attachment=attachment,
                    source_path=snapshot.path,
                    filename=filename,
                    title=f"{metadata.title or filename} [source HTML]",
                    content_type="text/html",
                    probe_attachment_key=_inventory_probe_attachment_key(inventory),
                    dedupe_prefix="full-text-html",
                    source_sha256=expected_source.sha256,
                ),
                failure_reason="relay_attachment_invalid_result",
            )
        except Exception as exc:
            relay_result = _local_failure("relay_attachment_failed", exc)
        except BaseException:
            snapshot.close()
            raise
        if relay_result.get("ok") is not True:
            snapshot.close()
            return {
                "ok": False,
                "kind": "html",
                "status": "relay_attachment_failed",
                "source": html,
                "raw_source_path": prepared.get("raw_source_path"),
                "article_standard": prepared.get("article_standard"),
                "attachment_source_path": str(attachment_source_path),
                "embedded_assets": embedded_assets,
                "relay": relay_result,
                "local_copy": {
                    "ok": False,
                    "skipped": True,
                    "reason": "relay_attachment_failed",
                },
            }
        try:
            source_after_relay = _stable_file_fingerprint(
                snapshot.path,
                max_bytes=HTML_ATTACHMENT_MAX_OUTPUT_BYTES,
            )
        except OSError:
            source_after_relay = None
        except BaseException:
            snapshot.close()
            raise
        if source_after_relay != snapshot_source:
            snapshot.close()
            return {
                "ok": False,
                "kind": "html",
                "status": "attachment_snapshot_changed_after_relay",
                "source": html,
                "raw_source_path": prepared.get("raw_source_path"),
                "article_standard": prepared.get("article_standard"),
                "attachment_source_path": str(attachment_source_path),
                "embedded_assets": embedded_assets,
                "relay": relay_result,
                "local_copy": {
                    "ok": False,
                    "skipped": True,
                    "reason": "attachment_snapshot_changed_after_relay",
                },
            }
        try:
            local_copy = write_parent_attachment_local_copy(
                attachment=attachment,
                source_path=snapshot.path,
                filename=filename,
                relay_result=relay_result,
                expected_source=snapshot_source,
            )
        except Exception as exc:
            local_copy = _local_failure("local_copy_failed", exc)
        except BaseException:
            snapshot.close()
            raise
        snapshot.close()
        if local_copy.get("ok") is True:
            try:
                local_metadata = _local_metadata_result(
                    sync_parent_attachment_local(
                        metadata=metadata,
                        attachment=attachment,
                        filename=filename,
                        title=f"{metadata.title or filename} [source HTML]",
                        content_type="text/html",
                        relay_result=relay_result,
                    ),
                )
            except Exception as exc:
                local_metadata = _local_failure("local_metadata_failed", exc)
        else:
            local_metadata = {
                "ok": False,
                "skipped": True,
                "reason": "local_copy_failed",
            }
        result: dict[str, Any] = {
            "ok": local_copy.get("ok") is True,
            "kind": "html",
            "source": html,
            "raw_source_path": prepared.get("raw_source_path"),
            "article_standard": prepared.get("article_standard"),
            "raw_html_fallback": prepared.get("raw_html_fallback") is True,
            "attachment_source_path": str(attachment_source_path),
            "embedded_assets": embedded_assets,
            "relay": relay_result,
            "local_copy": local_copy,
            "local_metadata": local_metadata,
        }
        if local_copy.get("ok") is not True:
            result["status"] = "local_copy_failed"
            return result
        if self.enqueue_html_for_translation is not None:
            try:
                translation_enqueue = _downstream_result(
                    self.enqueue_html_for_translation(
                        metadata=metadata,
                        attachment=attachment,
                        source_path=Path(str(local_copy["path"])),
                        relay_result=relay_result,
                    ),
                    failure_reason="translation_enqueue_invalid_result",
                )
            except Exception as exc:
                translation_enqueue = _local_failure("translation_enqueue_failed", exc)
            result["translation_enqueue"] = translation_enqueue
            if translation_enqueue.get("ok") is not True:
                result["ok"] = False
                result["status"] = "translation_enqueue_failed"
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
        package_value = standardize_native_html_download(
            download,
            metadata=metadata,
            package_root=raw_source_path.parent / "article_packages",
            source_context="full_text_attachment",
        )
        if not isinstance(package_value, dict):
            package = _downstream_result(
                package_value,
                failure_reason="article_standard_invalid_result",
            )
            html["standard_package"] = package
            return {
                "ok": False,
                "kind": "html",
                "status": "article_standard_invalid_result",
                "sourcePath": str(raw_source_path),
                "source": html,
                "article_standard": package,
            }
        package = package_value
        html["standard_package"] = package
        article_html_path = Path(str(package.get("article_html_path") or ""))
        validated_path = (
            validated_article_package_html_path(article_html_path)
            if package.get("ok") is True
            else None
        )
        if validated_path is not None:
            html["standard_article_html_path"] = str(validated_path)
            return {
                "ok": True,
                "source_path": validated_path,
                "raw_source_path": str(raw_source_path),
                "article_standard": package,
                "raw_html_fallback": False,
            }
        if package.get("ok") is True:
            return {
                "ok": False,
                "kind": "html",
                "status": "article_package_integrity_failed",
                "sourcePath": str(article_html_path),
                "source": html,
                "article_standard": package,
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
        source_path = _download_output_path(pdf)
        if source_path is None:
            return {
                "ok": False,
                "kind": "pdf",
                "status": "invalid_source_path",
                "source": pdf,
            }
        if not source_path.exists():
            return {
                "ok": False,
                "status": "local_source_missing",
                "sourcePath": str(source_path),
            }
        try:
            expected_source = _stable_file_fingerprint(
                source_path,
                max_bytes=None,
            )
        except OSError as exc:
            return {
                "ok": False,
                "kind": "pdf",
                "status": "attachment_source_unstable_before_relay",
                "source": pdf,
                "sourcePath": str(source_path),
                "error": f"{type(exc).__name__}: {exc}"[:500],
            }
        filename = _full_text_attachment_filename(
            source_path=source_path,
            title=metadata.title,
            suffix="FULL TEXT",
            extension=".pdf",
        )
        try:
            snapshot = _create_parent_attachment_source_snapshot(
                source_path,
                expected_source=expected_source,
                max_bytes=None,
            )
        except OSError as exc:
            return {
                "ok": False,
                "kind": "pdf",
                "status": "attachment_snapshot_failed",
                "source": pdf,
                "sourcePath": str(source_path),
                "source_sha256": expected_source.sha256,
                "error": f"{type(exc).__name__}: {exc}"[:500],
            }
        snapshot_source = snapshot.fingerprint
        try:
            self._ensure_active()
        except BaseException:
            snapshot.close()
            raise
        try:
            relay_result = _downstream_result(
                self.create_parent_attachment(
                    metadata=metadata,
                    attachment=attachment,
                    source_path=snapshot.path,
                    filename=filename,
                    title=f"{metadata.title or filename} [full text]",
                    content_type="application/pdf",
                    probe_attachment_key=_inventory_probe_attachment_key(inventory),
                    dedupe_prefix="full-text-pdf",
                    source_sha256=expected_source.sha256,
                ),
                failure_reason="relay_attachment_invalid_result",
            )
        except Exception as exc:
            relay_result = _local_failure("relay_attachment_failed", exc)
        except BaseException:
            snapshot.close()
            raise
        if relay_result.get("ok") is not True:
            snapshot.close()
            return {
                "ok": False,
                "kind": "pdf",
                "status": "relay_attachment_failed",
                "source": pdf,
                "relay": relay_result,
                "local_copy": {
                    "ok": False,
                    "skipped": True,
                    "reason": "relay_attachment_failed",
                },
            }
        try:
            source_after_relay = _stable_file_fingerprint(
                snapshot.path,
                max_bytes=None,
            )
        except OSError:
            source_after_relay = None
        except BaseException:
            snapshot.close()
            raise
        if source_after_relay != snapshot_source:
            snapshot.close()
            return {
                "ok": False,
                "kind": "pdf",
                "status": "attachment_snapshot_changed_after_relay",
                "source": pdf,
                "sourcePath": str(source_path),
                "source_sha256": expected_source.sha256,
                "relay": relay_result,
                "local_copy": {
                    "ok": False,
                    "skipped": True,
                    "reason": "attachment_snapshot_changed_after_relay",
                },
            }
        try:
            local_copy = write_parent_attachment_local_copy(
                attachment=attachment,
                source_path=snapshot.path,
                filename=filename,
                relay_result=relay_result,
                expected_source=snapshot_source,
                max_source_bytes=None,
            )
        except Exception as exc:
            local_copy = _local_failure("local_copy_failed", exc)
        except BaseException:
            snapshot.close()
            raise
        snapshot.close()
        if local_copy.get("ok") is True:
            try:
                local_metadata = _local_metadata_result(
                    sync_parent_attachment_local(
                        metadata=metadata,
                        attachment=attachment,
                        filename=filename,
                        title=f"{metadata.title or filename} [full text]",
                        content_type="application/pdf",
                        relay_result=relay_result,
                    ),
                )
            except Exception as exc:
                local_metadata = _local_failure("local_metadata_failed", exc)
        else:
            local_metadata = {
                "ok": False,
                "skipped": True,
                "reason": "local_copy_failed",
            }
        result: dict[str, Any] = {
            "ok": local_copy.get("ok") is True,
            "kind": "pdf",
            "source": pdf,
            "relay": relay_result,
            "source_sha256": expected_source.sha256,
            "local_copy": local_copy,
            "local_metadata": local_metadata,
        }
        if local_copy.get("ok") is not True:
            result["status"] = "local_copy_failed"
            result["pdf_enqueue"] = {
                "ok": False,
                "skipped": True,
                "reason": "local_copy_failed",
            }
            return result
        if _download_needs_ocr(pdf):
            enqueue_key = "ocr_enqueue"
            enqueue_status = "ocr_enqueue_failed"
            try:
                enqueue_result = _downstream_result(
                    self.enqueue_pdf_for_ocr(
                        metadata=metadata,
                        attachment=attachment,
                        source_path=Path(str(local_copy["path"])),
                        relay_result=relay_result,
                    ),
                    failure_reason="ocr_enqueue_invalid_result",
                )
            except Exception as exc:
                enqueue_result = _local_failure("ocr_enqueue_failed", exc)
        else:
            enqueue_key = "html_enqueue"
            enqueue_status = "html_enqueue_failed"
            try:
                enqueue_result = _downstream_result(
                    self.enqueue_pdf_for_html(
                        metadata=metadata,
                        attachment=attachment,
                        source_path=Path(str(local_copy["path"])),
                        relay_result=relay_result,
                    ),
                    failure_reason="html_enqueue_invalid_result",
                )
            except Exception as exc:
                enqueue_result = _local_failure("html_enqueue_failed", exc)
        result[enqueue_key] = enqueue_result
        if enqueue_result.get("ok") is not True:
            result["ok"] = False
            result["status"] = enqueue_status
        return result


def _local_failure(reason: str, exc: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "reason": reason,
        "error": f"{type(exc).__name__}: {exc}",
    }


def _downstream_result(
    value: object,
    *,
    failure_reason: str,
) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {
        "ok": False,
        "reason": failure_reason,
        "error": f"Expected a mapping result, got {type(value).__name__}.",
    }


def _local_metadata_result(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return _downstream_result(
            value,
            failure_reason="local_metadata_sync_invalid_result",
        )
    if value.get("ok") is True or value.get("ok") is False:
        return value
    return {
        "ok": False,
        "reason": "local_metadata_sync_invalid_result",
        "error": ("Expected a mapping result with an exact boolean ok field."),
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
        "has_html": inventory.get("has_html") is True,
        "has_pdf": inventory.get("has_pdf") is True,
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
        "has_html": inventory.get("has_html") is True,
        "has_source_html": inventory.get("has_source_html") is True,
        "source": html,
        "source_html_cleanup": cleanup,
    }


def _with_pdf_attachment_result(
    *,
    html_result: dict[str, Any],
    pdf_result: dict[str, Any],
) -> dict[str, Any]:
    result = dict(html_result)
    result["ok"] = html_result.get("ok") is True and pdf_result.get("ok") is True
    result["attached_kinds"] = ["html", "pdf"]
    result["attachments"] = [html_result, pdf_result]
    result["html_attachment"] = html_result
    result["pdf_attachment"] = pdf_result
    if "relay" in pdf_result:
        result["pdf_relay"] = pdf_result["relay"]
    if "local_copy" in pdf_result:
        result["pdf_local_copy"] = pdf_result["local_copy"]
    return result


@dataclass(frozen=True)
class _OwnedParentAttachmentSnapshot:
    path: Path
    fingerprint: _FileFingerprint

    def close(self) -> None:
        _unlink_owned_regular_file(
            self.path,
            device=self.fingerprint.device,
            inode=self.fingerprint.inode,
        )


def _create_parent_attachment_source_snapshot(
    source_path: Path,
    *,
    expected_source: _FileFingerprint,
    max_bytes: int | None,
) -> _OwnedParentAttachmentSnapshot:
    if source_path.suffix.casefold() in {".html", ".htm", ".xhtml"}:
        snapshot_dir = _embedded_html_snapshot_dir(source_path)
    else:
        snapshot_dir = source_path.parent
    if snapshot_dir.is_symlink() or not snapshot_dir.is_dir():
        raise OSError(
            f"Parent attachment snapshot directory is invalid: {snapshot_dir}"
        )
    snapshot_path = snapshot_dir / (
        f".z2m-parent-attachment-snapshot-{uuid.uuid4().hex}{source_path.suffix}"
    )
    publication = _copy_file_bounded_with_owner(
        source_path,
        snapshot_path,
        max_bytes=max_bytes,
    )
    if publication is None:
        raise OSError(
            f"Attachment source changed while creating snapshot: {source_path}"
        )
    copied_source = publication.source
    created_owner = (publication.target_device, publication.target_inode)
    try:
        if copied_source != expected_source:
            raise OSError(
                f"Attachment source changed before snapshot publication: {source_path}"
            )
        snapshot_fingerprint = _stable_file_fingerprint(
            snapshot_path,
            max_bytes=max_bytes,
        )
        if not _same_file_content(snapshot_fingerprint, expected_source):
            raise OSError(
                f"Attachment snapshot failed integrity check: {snapshot_path}"
            )
    except BaseException:
        _unlink_owned_regular_file(
            snapshot_path,
            device=created_owner[0],
            inode=created_owner[1],
        )
        raise
    return _OwnedParentAttachmentSnapshot(
        path=snapshot_path,
        fingerprint=snapshot_fingerprint,
    )


def write_parent_attachment_local_copy(
    *,
    attachment: LocalAttachment,
    source_path: Path,
    filename: str,
    relay_result: dict[str, Any],
    expected_source: _FileFingerprint | None = None,
    max_source_bytes: int | None = HTML_ATTACHMENT_MAX_OUTPUT_BYTES,
) -> dict[str, Any]:
    new_key = _relay_attachment_key(relay_result)
    if not new_key:
        raise RuntimeError(
            "zotero-file-relay parent attachment did not return a usable attachment key."
        )
    target_dir = _validated_attachment_storage_dir(attachment.storage_dir, new_key)
    target_path = target_dir / Path(filename).name
    temp_path = target_dir / f".{target_path.name}.full-text-tmp-{uuid.uuid4().hex}"
    published_owner: tuple[int, int] | None = None
    try:
        before = _stable_file_fingerprint(
            source_path,
            max_bytes=max_source_bytes,
        )
        if expected_source is not None and before != expected_source:
            raise OSError(f"Attachment source changed before local copy: {source_path}")
        effective_source = expected_source or before
        copied_source = _copy_file_bounded(
            source_path,
            temp_path,
            max_bytes=max_source_bytes,
        )
        if copied_source is None or copied_source != effective_source:
            raise OSError(f"Attachment source changed during local copy: {source_path}")
        after = _stable_file_fingerprint(
            source_path,
            max_bytes=max_source_bytes,
        )
        copied = _stable_file_fingerprint(
            temp_path,
            max_bytes=max_source_bytes,
        )
        if after != effective_source or not _same_file_content(
            copied,
            effective_source,
        ):
            raise OSError(f"Attachment source changed during local copy: {source_path}")
        current_target_dir = _validated_attachment_storage_dir(
            attachment.storage_dir,
            new_key,
        )
        if current_target_dir != target_dir:
            raise OSError(
                f"Attachment storage directory changed during copy: {target_dir}"
            )
        reused = False
        try:
            os.link(temp_path, target_path)
        except FileExistsError:
            existing = _stable_file_fingerprint(
                target_path,
                max_bytes=max_source_bytes,
            )
            if not _same_file_content(existing, effective_source):
                raise FileExistsError(
                    "Local attachment target already exists with different content: "
                    f"{target_path}"
                )
            reused = True
        else:
            published_owner = (copied.device, copied.inode)
        try:
            published = _stable_file_fingerprint(
                target_path,
                max_bytes=max_source_bytes,
            )
            if not _same_file_content(published, effective_source):
                raise OSError(
                    f"Local attachment copy failed integrity check: {target_path}"
                )
        except OSError:
            if published_owner is not None:
                _unlink_owned_regular_file(
                    target_path,
                    device=published_owner[0],
                    inode=published_owner[1],
                )
            raise
        return {
            "ok": True,
            "attachmentKey": new_key,
            "path": str(target_path),
            "reused": reused,
        }
    finally:
        _unlink_temporary_file(temp_path)


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


def _nonempty_string(value: object) -> str | None:
    text = value.strip() if isinstance(value, str) else ""
    return text or None


def _download_output_path(item: dict[str, Any]) -> Path | None:
    output_path = _nonempty_string(item.get("output_path"))
    return Path(output_path) if output_path is not None else None


def _first_successful_download(value: object) -> dict[str, Any] | None:
    if not isinstance(value, list):
        return None
    for item in value:
        if not isinstance(item, dict):
            continue
        if item.get("ok") is True and _download_output_path(item) is not None:
            return item
    return None


def _html_attachment_preferred_source_path(item: dict[str, Any]) -> Path:
    standard_path = _nonempty_string(item.get("standard_article_html_path"))
    if standard_path:
        return Path(standard_path)
    standard_package = item.get("standard_package")
    if isinstance(standard_package, dict):
        package_path = _nonempty_string(standard_package.get("article_html_path"))
        if package_path:
            return Path(package_path)
    return _download_output_path(item) or Path()


def _html_attachment_raw_source_path(item: dict[str, Any]) -> Path:
    return _download_output_path(item) or Path()


def _html_attachment_standard_package(item: dict[str, Any]) -> dict[str, Any] | None:
    package = item.get("standard_package")
    return package if isinstance(package, dict) else None


def _html_attachment_existing_standard_path(item: dict[str, Any]) -> Path | None:
    package = _html_attachment_standard_package(item)
    candidates = [_nonempty_string(item.get("standard_article_html_path"))]
    if package is not None and package.get("ok") is True:
        candidates.append(_nonempty_string(package.get("article_html_path")))
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        validated = validated_article_package_html_path(path)
        if validated is not None:
            return validated
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
        if (
            item.get("ok") is True
            and _download_output_path(item) is not None
            and verdict.get("ok") is True
        ):
            candidates.append(item)
    if not candidates:
        return None
    return max(candidates, key=_html_download_score)


def _html_download_score(item: dict[str, Any]) -> tuple[int, int, int, int, int]:
    article = item.get("article")
    article_data = article if isinstance(article, dict) else {}
    markers = _normalized_string_list(article_data.get("markers"))
    section_markers = _normalized_string_list(article_data.get("section_markers"))
    url = " ".join(
        value
        for value in (
            _nonempty_string(item.get("url")),
            _nonempty_string(item.get("final_url")),
        )
        if value is not None
    ).casefold()
    kind = (_nonempty_string(item.get("kind")) or "").casefold()
    source = (_nonempty_string(item.get("source")) or "").casefold()
    raw_text_chars = article_data.get("text_chars")
    text_chars = (
        raw_text_chars
        if isinstance(raw_text_chars, int)
        and not isinstance(raw_text_chars, bool)
        and raw_text_chars >= 0
        else 0
    )

    full_article_bonus = 0
    if kind == "html":
        full_article_bonus += 2
    if "/html/" in url:
        full_article_bonus += 2
    if source == "arxiv":
        full_article_bonus += 1
    if markers.intersection(
        {"article_tag", "arxiv_ltx_document", "arxiv_ltx_bibliography"}
    ):
        full_article_bonus += 2
    if section_markers.intersection({"methods", "results", "discussion", "conclusion"}):
        full_article_bonus += 1

    landing_penalty = 1 if kind == "landing" else 0
    references_bonus = (
        1 if "references" in markers or "references" in section_markers else 0
    )
    return (
        full_article_bonus,
        references_bonus,
        text_chars,
        -landing_penalty,
        -len(_nonempty_string(item.get("output_path")) or ""),
    )


def _normalized_string_list(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {
        item.strip().casefold()
        for item in value
        if isinstance(item, str) and item.strip()
    }


_is_arxiv_abs_landing_download = is_arxiv_abs_landing_download


def _html_attachment_source_with_embedded_assets(
    source_path: Path,
    *,
    max_source_bytes: int = HTML_ATTACHMENT_MAX_SOURCE_BYTES,
    max_asset_bytes: int = HTML_ATTACHMENT_MAX_ASSET_BYTES,
    max_total_asset_bytes: int = HTML_ATTACHMENT_MAX_TOTAL_ASSET_BYTES,
    max_assets: int = HTML_ATTACHMENT_MAX_ASSETS,
    max_scanned_assets: int = HTML_ATTACHMENT_MAX_SCANNED_ASSETS,
    max_output_bytes: int = HTML_ATTACHMENT_MAX_OUTPUT_BYTES,
) -> tuple[Path, dict[str, Any]]:
    if source_path.suffix.casefold() not in {".html", ".htm", ".xhtml"}:
        return source_path, {"enabled": False, "reason": "not_html"}

    if source_path.is_symlink():
        return source_path, {
            "enabled": False,
            "failed": True,
            "reason": "source_symlink",
        }
    assets_dir = source_path.parent / f"{source_path.stem}_assets"
    if not assets_dir.is_dir() and source_path.name == "article.html":
        standard_assets_dir = source_path.parent / "assets"
        if standard_assets_dir.is_dir():
            assets_dir = standard_assets_dir

    source_bytes = _file_size_or_zero(source_path)
    if source_bytes > max(max_source_bytes, 0):
        return source_path, {
            "enabled": False,
            "failed": True,
            "reason": "source_too_large",
            "assets_dir": str(assets_dir),
            "source_bytes": _file_size_or_zero(source_path),
            "max_source_bytes": max(max_source_bytes, 0),
        }
    try:
        source_snapshot = _read_file_snapshot_bounded(
            source_path,
            max_bytes=max_source_bytes,
        )
    except OSError as exc:
        return source_path, {
            "enabled": False,
            "failed": True,
            "reason": "source_unstable",
            "assets_dir": str(assets_dir),
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }

    source_fingerprint = source_snapshot.fingerprint
    html_text = source_snapshot.payload.decode("utf-8", errors="replace")
    del source_snapshot
    unresolved_source_refs = _unresolved_attachment_resource_refs(html_text)
    source_reference_paths = {
        normalized
        for ref in _html_attachment_resource_refs(html_text)
        if (normalized := _normalized_local_asset_reference_path(ref)) is not None
    }
    if not assets_dir.is_dir():
        if unresolved_source_refs:
            return source_path, _unresolved_local_assets_report(
                assets_dir=assets_dir,
                unresolved_refs=unresolved_source_refs,
            )
        return source_path, {"enabled": False, "reason": "assets_dir_missing"}
    if assets_dir.is_symlink():
        return source_path, {
            "enabled": False,
            "failed": True,
            "reason": "assets_dir_symlink",
            "assets_dir": str(assets_dir),
        }

    try:
        asset_files, scan_truncated = _local_asset_candidates(
            assets_dir,
            max_scanned_assets=max_scanned_assets,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return source_path, {
            "enabled": False,
            "failed": True,
            "reason": "asset_scan_failed",
            "assets_dir": str(assets_dir),
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }
    if scan_truncated:
        return source_path, {
            "enabled": False,
            "failed": True,
            "reason": "asset_scan_limit",
            "assets_dir": str(assets_dir),
            "asset_count": len(asset_files),
            "max_scanned_assets": max(max_scanned_assets, 0),
        }
    if not asset_files:
        if unresolved_source_refs:
            return source_path, _unresolved_local_assets_report(
                assets_dir=assets_dir,
                unresolved_refs=unresolved_source_refs,
            )
        return source_path, {
            "enabled": False,
            "reason": "assets_empty",
            "assets_dir": str(assets_dir),
        }

    try:
        budget = _LocalAssetEmbeddingBudget(
            assets_dir=assets_dir,
            max_asset_bytes=max_asset_bytes,
            max_total_bytes=max_total_asset_bytes,
            max_assets=max_assets,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return source_path, {
            "enabled": False,
            "failed": True,
            "reason": "asset_root_unstable",
            "assets_dir": str(assets_dir),
            "asset_count": len(asset_files),
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }
    css_text_by_rel: dict[str, str] = {}
    data_uri_by_rel: dict[str, str] = {}
    missing_local_refs: list[str] = []

    for asset in asset_files:
        if _is_css_asset(asset):
            continue
        if budget.relative_path(asset) is None:
            continue
        rel = _asset_html_relpath(assets_dir, asset)
        if _normalized_local_asset_reference_path(rel) not in source_reference_paths:
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
        if _normalized_local_asset_reference_path(rel) not in source_reference_paths:
            continue
        try:
            css_text, css_missing = _css_with_embedded_local_assets(
                asset,
                assets_dir=assets_dir,
                budget=budget,
                max_output_chars=max(max_output_bytes, 0),
            )
        except _HTMLAttachmentOutputLimitExceeded:
            return source_path, _embedding_output_too_large_report(
                assets_dir=assets_dir,
                asset_count=len(asset_files),
                max_output_bytes=max_output_bytes,
                budget=budget,
            )
        missing_local_refs.extend(css_missing)
        if css_text is not None:
            css_text_by_rel[rel] = css_text

    if not data_uri_by_rel and not css_text_by_rel:
        if unresolved_source_refs:
            return source_path, _unresolved_local_assets_report(
                assets_dir=assets_dir,
                unresolved_refs=unresolved_source_refs,
                asset_count=len(asset_files),
                budget=budget,
                missing_local_refs=missing_local_refs,
            )
        return source_path, {
            "enabled": False,
            "reason": "no_embeddable_assets",
            "assets_dir": str(assets_dir),
            "asset_count": len(asset_files),
            "skipped_asset_count": budget.skipped_count,
            "skipped_assets": budget.skipped[:20],
        }

    try:
        rewritten = _replace_stylesheet_links_with_style_tags(
            html_text,
            css_text_by_rel,
            max_output_chars=max(max_output_bytes, 0),
        )
        rewritten = _replace_style_imports_with_embedded_css(
            rewritten,
            css_text_by_rel,
            max_output_chars=max(max_output_bytes, 0),
        )
        rewritten = _replace_html_asset_references(
            rewritten,
            data_uri_by_rel,
            max_output_chars=max(max_output_bytes, 0),
        )
    except _HTMLAttachmentOutputLimitExceeded:
        return source_path, _embedding_output_too_large_report(
            assets_dir=assets_dir,
            asset_count=len(asset_files),
            max_output_bytes=max_output_bytes,
            budget=budget,
        )

    unresolved_rewritten_refs = _unresolved_attachment_resource_refs(rewritten)
    if unresolved_rewritten_refs:
        return source_path, _unresolved_local_assets_report(
            assets_dir=assets_dir,
            unresolved_refs=unresolved_rewritten_refs,
            asset_count=len(asset_files),
            budget=budget,
            missing_local_refs=missing_local_refs,
        )

    if not _path_fingerprint_matches(
        source_path,
        source_fingerprint,
        max_bytes=max_source_bytes,
    ):
        return source_path, {
            "enabled": False,
            "failed": True,
            "reason": "source_changed",
            "assets_dir": str(assets_dir),
        }
    changed_asset = budget.changed_input_path()
    if changed_asset is not None:
        return source_path, {
            "enabled": False,
            "failed": True,
            "reason": "asset_changed",
            "assets_dir": str(assets_dir),
            "asset_path": str(changed_asset),
        }
    try:
        embedded_path, output_fingerprint, cache_reused = (
            _publish_embedded_html_snapshot(
                source_path,
                rewritten,
                max_bytes=max_output_bytes,
            )
        )
    except OSError as exc:
        return source_path, {
            "enabled": False,
            "failed": True,
            "reason": "output_publish_failed",
            "assets_dir": str(assets_dir),
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }
    source_changed = not _path_fingerprint_matches(
        source_path,
        source_fingerprint,
        max_bytes=max_source_bytes,
    )
    changed_asset = budget.changed_input_path()
    if source_changed or changed_asset is not None:
        return source_path, {
            "enabled": False,
            "failed": True,
            "reason": "source_changed" if source_changed else "asset_changed",
            "assets_dir": str(assets_dir),
            "asset_path": str(changed_asset) if changed_asset is not None else "",
        }
    return embedded_path, {
        "enabled": True,
        "source_path": str(source_path),
        "output_path": str(embedded_path),
        "source": {
            "bytes": source_fingerprint.bytes,
            "sha256": source_fingerprint.sha256,
        },
        "output": {
            "bytes": output_fingerprint.bytes,
            "sha256": output_fingerprint.sha256,
        },
        "cache_reused": cache_reused,
        "assets_dir": str(assets_dir),
        "asset_count": len(asset_files),
        "embedded_assets": budget.embedded_asset_count,
        "embedded_stylesheets": budget.embedded_stylesheet_count,
        "embedded_source_bytes": budget.total_bytes,
        "missing_local_refs": missing_local_refs[:20],
        "skipped_asset_count": budget.skipped_count,
        "skipped_assets": budget.skipped[:20],
    }


def _unresolved_attachment_resource_refs(html_text: str) -> list[str]:
    unresolved: list[str] = []
    for raw_ref in _html_attachment_resource_refs(html_text):
        value = html.unescape(str(raw_ref)).strip().strip("\"'")
        if not value:
            continue
        if _is_external_or_data_url(value):
            continue
        parsed = _safe_urlparse(value)
        if parsed is None:
            unresolved.append(value)
            continue
        local_path = urllib.parse.unquote(parsed.path).strip()
        if local_path:
            unresolved.append(value)
    return sorted(dict.fromkeys(unresolved), key=str.casefold)


def _unresolved_local_assets_report(
    *,
    assets_dir: Path,
    unresolved_refs: list[str],
    asset_count: int = 0,
    budget: _LocalAssetEmbeddingBudget | None = None,
    missing_local_refs: list[str] | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "enabled": False,
        "failed": True,
        "reason": "unresolved_local_assets",
        "assets_dir": str(assets_dir),
        "asset_count": asset_count,
        "unresolved_local_refs": unresolved_refs[:20],
    }
    if budget is not None:
        report.update(
            {
                "embedded_assets": budget.embedded_asset_count,
                "embedded_stylesheets": budget.embedded_stylesheet_count,
                "embedded_source_bytes": budget.total_bytes,
                "missing_local_refs": (missing_local_refs or [])[:20],
                "skipped_asset_count": budget.skipped_count,
                "skipped_assets": budget.skipped[:20],
            }
        )
    return report


def _embedding_output_too_large_report(
    *,
    assets_dir: Path,
    asset_count: int,
    max_output_bytes: int,
    budget: _LocalAssetEmbeddingBudget,
) -> dict[str, Any]:
    return {
        "enabled": False,
        "failed": True,
        "reason": "output_too_large",
        "assets_dir": str(assets_dir),
        "asset_count": asset_count,
        "max_output_bytes": max(max_output_bytes, 0),
        "embedded_assets": budget.embedded_asset_count,
        "embedded_stylesheets": budget.embedded_stylesheet_count,
        "embedded_source_bytes": budget.total_bytes,
        "skipped_asset_count": budget.skipped_count,
        "skipped_assets": budget.skipped[:20],
    }


def _path_fingerprint_matches(
    path: Path,
    expected: _FileFingerprint,
    *,
    max_bytes: int,
) -> bool:
    try:
        return _stable_file_fingerprint(path, max_bytes=max_bytes) == expected
    except OSError:
        return False


def _matching_content_fingerprint(
    path: Path,
    expected: _FileFingerprint,
    *,
    max_bytes: int,
) -> _FileFingerprint | None:
    last_error: OSError | None = None
    for attempt in range(HTML_ATTACHMENT_PUBLICATION_VERIFY_ATTEMPTS):
        try:
            current = _stable_file_fingerprint(path, max_bytes=max_bytes)
        except OSError as exc:
            last_error = exc
            current = None
        else:
            last_error = None
        if current is not None and _same_file_content(current, expected):
            return current
        if attempt + 1 < HTML_ATTACHMENT_PUBLICATION_VERIFY_ATTEMPTS:
            time.sleep(HTML_ATTACHMENT_PUBLICATION_VERIFY_DELAY_SECONDS)
    if last_error is not None:
        raise last_error
    return None


def _unlink_temporary_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def _publish_embedded_html_snapshot(
    source_path: Path,
    html_text: str,
    *,
    max_bytes: int,
) -> tuple[Path, _FileFingerprint, bool]:
    snapshot_dir = _embedded_html_snapshot_dir(source_path)
    temp_path = snapshot_dir / (f".z2m-embedded-tmp-{uuid.uuid4().hex}")
    cache_reused: bool | None = None
    try:
        fingerprint = _write_text_file_bounded(
            temp_path,
            html_text,
            max_bytes=max_bytes,
        )
        target_path = snapshot_dir / (f"z2m_embedded.{fingerprint.sha256}.html")
        try:
            cache_reused = _link_or_repair_embedded_snapshot(
                temp_path,
                target_path,
                expected=fingerprint,
                max_bytes=max_bytes,
            )
            published = _matching_content_fingerprint(
                target_path,
                fingerprint,
                max_bytes=max_bytes,
            )
            if published is None:
                raise OSError(
                    f"Published embedded HTML does not match snapshot: {target_path}"
                )
        except OSError:
            if cache_reused is False:
                _unlink_owned_regular_file(
                    target_path,
                    device=fingerprint.device,
                    inode=fingerprint.inode,
                )
            raise
        return target_path, published, cache_reused
    finally:
        _unlink_temporary_file(temp_path)


def _link_or_repair_embedded_snapshot(
    temp_path: Path,
    target_path: Path,
    *,
    expected: _FileFingerprint,
    max_bytes: int,
) -> bool:
    try:
        os.link(temp_path, target_path)
    except FileExistsError:
        last_replace_error: PermissionError | None = None
        for attempt in range(HTML_ATTACHMENT_PUBLICATION_VERIFY_ATTEMPTS):
            try:
                current = _stable_file_fingerprint(
                    target_path,
                    max_bytes=max_bytes,
                )
            except OSError:
                current = None
                if target_path.is_dir() and not target_path.is_symlink():
                    raise OSError(
                        f"Embedded HTML cache target is a directory: {target_path}"
                    )
                if attempt + 1 < HTML_ATTACHMENT_PUBLICATION_VERIFY_ATTEMPTS:
                    time.sleep(HTML_ATTACHMENT_PUBLICATION_VERIFY_DELAY_SECONDS)
                    continue
            if current is not None and _same_file_content(current, expected):
                return True
            if target_path.is_dir() and not target_path.is_symlink():
                raise OSError(
                    f"Embedded HTML cache target is a directory: {target_path}"
                )
            try:
                os.replace(temp_path, target_path)
                return False
            except PermissionError as exc:
                last_replace_error = exc
                if attempt + 1 < HTML_ATTACHMENT_PUBLICATION_VERIFY_ATTEMPTS:
                    time.sleep(HTML_ATTACHMENT_PUBLICATION_VERIFY_DELAY_SECONDS)
        try:
            current = _matching_content_fingerprint(
                target_path,
                expected,
                max_bytes=max_bytes,
            )
        except OSError:
            current = None
        if current is not None:
            return True
        if last_replace_error is not None:
            raise last_replace_error
    return False


def _embedded_html_snapshot_dir(source_path: Path) -> Path:
    if validated_article_package_html_path(source_path) is None:
        return source_path.parent
    logs_dir = source_path.parent / "logs"
    if logs_dir.is_symlink() or not logs_dir.is_dir():
        raise OSError(f"Sealed article package logs directory is invalid: {logs_dir}")
    snapshot_dir = logs_dir / "attachment_snapshots"
    snapshot_dir.mkdir(exist_ok=True)
    if snapshot_dir.is_symlink() or not snapshot_dir.is_dir():
        raise OSError(f"Embedded HTML snapshot directory is invalid: {snapshot_dir}")
    try:
        logs_root = logs_dir.resolve(strict=True)
        snapshot_dir.resolve(strict=True).relative_to(logs_root)
    except (OSError, ValueError) as exc:
        raise OSError(
            f"Embedded HTML snapshot directory escapes package logs: {snapshot_dir}"
        ) from exc
    return snapshot_dir


_HTML_RAW_TEXT_ELEMENTS = frozenset(
    {
        "iframe",
        "noembed",
        "noframes",
        "plaintext",
        "script",
        "style",
        "textarea",
        "title",
        "xmp",
    }
)
_HTML_RESOURCE_ATTRIBUTES: dict[str, frozenset[str]] = {
    "a": frozenset({"href"}),
    "area": frozenset({"href"}),
    "audio": frozenset({"src"}),
    "body": frozenset({"background"}),
    "embed": frozenset({"src"}),
    "feimage": frozenset({"href", "xlink:href"}),
    "iframe": frozenset({"src"}),
    "image": frozenset({"href", "xlink:href"}),
    "img": frozenset({"src", "srcset"}),
    "input": frozenset({"src"}),
    "link": frozenset({"href", "imagesrcset"}),
    "object": frozenset({"data"}),
    "script": frozenset({"src"}),
    "source": frozenset({"src", "srcset"}),
    "table": frozenset({"background"}),
    "td": frozenset({"background"}),
    "th": frozenset({"background"}),
    "track": frozenset({"src"}),
    "use": frozenset({"href", "xlink:href"}),
    "video": frozenset({"poster", "src"}),
}

_HTML_SRCSET_ATTRIBUTES = frozenset({"srcset", "imagesrcset"})


class _HTMLAttachmentOutputLimitExceeded(ValueError):
    pass


def _bounded_text_join(
    pieces: list[str],
    *,
    max_output_chars: int | None,
) -> str:
    if max_output_chars is not None:
        limit = max(max_output_chars, 0)
        total = 0
        for piece in pieces:
            total += len(piece)
            if total > limit:
                raise _HTMLAttachmentOutputLimitExceeded
    return "".join(pieces)


def _require_bounded_replacement_size(
    value: str,
    replacements: list[tuple[int, int, str]],
    *,
    max_output_chars: int | None,
) -> None:
    if max_output_chars is None:
        return
    projected = len(value) + sum(
        len(replacement) - (value_end - value_start)
        for value_start, value_end, replacement in replacements
    )
    if projected > max(max_output_chars, 0):
        raise _HTMLAttachmentOutputLimitExceeded


def _transform_html_contexts(
    html_text: str,
    *,
    replace_start_tag: Callable[[str, str], str] | None = None,
    replace_style_text: Callable[[str], str] | None = None,
    max_output_chars: int | None = None,
) -> str:
    pieces: list[str] = []
    cursor = 0
    while cursor < len(html_text):
        tag_start = html_text.find("<", cursor)
        if tag_start < 0:
            pieces.append(html_text[cursor:])
            break
        pieces.append(html_text[cursor:tag_start])
        if html_text.startswith("<!--", tag_start):
            comment_end = html_text.find("-->", tag_start + 4)
            if comment_end < 0:
                pieces.append(html_text[tag_start:])
                break
            comment_end += 3
            pieces.append(html_text[tag_start:comment_end])
            cursor = comment_end
            continue
        if html_text.startswith("<![CDATA[", tag_start):
            cdata_end = html_text.find("]]>", tag_start + 9)
            if cdata_end < 0:
                pieces.append(html_text[tag_start:])
                break
            cdata_end += 3
            pieces.append(html_text[tag_start:cdata_end])
            cursor = cdata_end
            continue
        next_index = tag_start + 1
        while next_index < len(html_text) and html_text[next_index].isspace():
            next_index += 1
        if next_index >= len(html_text) or (
            html_text[next_index] not in "!/?" and not html_text[next_index].isalpha()
        ):
            pieces.append("<")
            cursor = tag_start + 1
            continue
        if html_text.startswith("<?", tag_start):
            processing_end = html_text.find("?>", tag_start + 2)
            if processing_end < 0:
                pieces.append(html_text[tag_start:])
                break
            processing_end += 2
            pieces.append(html_text[tag_start:processing_end])
            cursor = processing_end
            continue
        tag_end = _html_markup_end(html_text, tag_start)
        if tag_end is None:
            pieces.append(html_text[tag_start:])
            break
        tag_text = html_text[tag_start:tag_end]
        identity = _html_tag_identity(tag_text)
        if identity is None:
            pieces.append(tag_text)
            cursor = tag_end
            continue
        closing, tag_name, _name_end = identity
        local_tag_name = tag_name.rsplit(":", 1)[-1]
        if closing:
            pieces.append(tag_text)
            cursor = tag_end
            continue
        pieces.append(
            replace_start_tag(tag_text, local_tag_name)
            if replace_start_tag is not None
            else tag_text
        )
        cursor = tag_end
        if local_tag_name not in _HTML_RAW_TEXT_ELEMENTS or _html_tag_is_self_closing(
            tag_text
        ):
            continue
        closing_match = re.search(
            rf"</\s*{re.escape(tag_name)}\b",
            html_text[cursor:],
            flags=re.IGNORECASE,
        )
        if closing_match is None:
            raw_text = html_text[cursor:]
            pieces.append(
                replace_style_text(raw_text)
                if local_tag_name == "style" and replace_style_text is not None
                else raw_text
            )
            break
        closing_start = cursor + closing_match.start()
        raw_text = html_text[cursor:closing_start]
        pieces.append(
            replace_style_text(raw_text)
            if local_tag_name == "style" and replace_style_text is not None
            else raw_text
        )
        closing_end = _html_markup_end(html_text, closing_start)
        if closing_end is None:
            pieces.append(html_text[closing_start:])
            break
        pieces.append(html_text[closing_start:closing_end])
        cursor = closing_end
    return _bounded_text_join(
        pieces,
        max_output_chars=max_output_chars,
    )


def _html_markup_end(html_text: str, tag_start: int) -> int | None:
    quote = ""
    index = tag_start + 1
    while index < len(html_text):
        character = html_text[index]
        if quote:
            if character == quote:
                quote = ""
        elif character in {'"', "'"}:
            quote = character
        elif character == ">":
            return index + 1
        index += 1
    return None


def _html_tag_identity(tag_text: str) -> tuple[bool, str, int] | None:
    match = re.match(
        r"<\s*(?P<closing>/?)\s*(?P<name>[A-Za-z][A-Za-z0-9:_.-]*)",
        tag_text,
    )
    if match is None:
        return None
    return (
        bool(match.group("closing")),
        match.group("name").casefold(),
        match.end("name"),
    )


def _html_tag_is_self_closing(tag_text: str) -> bool:
    return re.search(r"/\s*>\s*$", tag_text) is not None


def _html_attribute_spans(
    tag_text: str,
    *,
    name_end: int,
) -> list[tuple[str, int, int]]:
    spans: list[tuple[str, int, int]] = []
    index = name_end
    while index < len(tag_text):
        while index < len(tag_text) and tag_text[index].isspace():
            index += 1
        if index >= len(tag_text) or tag_text[index] in ">/":
            break
        attribute_start = index
        while index < len(tag_text) and (
            not tag_text[index].isspace() and tag_text[index] not in "=/>"
        ):
            index += 1
        if index == attribute_start:
            index += 1
            continue
        attribute_name = tag_text[attribute_start:index].casefold()
        while index < len(tag_text) and tag_text[index].isspace():
            index += 1
        if index >= len(tag_text) or tag_text[index] != "=":
            spans.append((attribute_name, index, index))
            continue
        index += 1
        while index < len(tag_text) and tag_text[index].isspace():
            index += 1
        if index >= len(tag_text):
            break
        if tag_text[index] in {'"', "'"}:
            quote = tag_text[index]
            value_start = index + 1
            value_end = tag_text.find(quote, value_start)
            if value_end < 0:
                value_end = len(tag_text)
                index = value_end
            else:
                index = value_end + 1
        else:
            value_start = index
            while index < len(tag_text) and (
                not tag_text[index].isspace() and tag_text[index] != ">"
            ):
                index += 1
            value_end = index
        spans.append((attribute_name, value_start, value_end))
    return spans


def _replace_html_asset_references(
    html_text: str,
    data_uri_by_rel: dict[str, str],
    *,
    max_output_chars: int | None = None,
) -> str:
    lookup = _asset_data_uri_lookup(data_uri_by_rel)
    if not lookup:
        return html_text

    def replace_tag(tag_text: str, tag_name: str) -> str:
        identity = _html_tag_identity(tag_text)
        if identity is None:
            return tag_text
        replacements: list[tuple[int, int, str]] = []
        resource_attributes = _HTML_RESOURCE_ATTRIBUTES.get(tag_name, frozenset())
        for attribute_name, value_start, value_end in _html_attribute_spans(
            tag_text,
            name_end=identity[2],
        ):
            value = tag_text[value_start:value_end]
            replacement: str | None = None
            if attribute_name == "style":
                replacement = _replace_css_asset_urls(
                    value,
                    lookup,
                    max_output_chars=max_output_chars,
                )
            elif attribute_name in resource_attributes:
                replacement = (
                    _replace_srcset_asset_urls(
                        value,
                        lookup,
                        max_output_chars=max_output_chars,
                    )
                    if attribute_name in _HTML_SRCSET_ATTRIBUTES
                    else _replace_asset_url(value, lookup)
                )
            if replacement is not None and replacement != value:
                replacements.append((value_start, value_end, replacement))
        _require_bounded_replacement_size(
            tag_text,
            replacements,
            max_output_chars=max_output_chars,
        )
        for value_start, value_end, replacement in reversed(replacements):
            tag_text = tag_text[:value_start] + replacement + tag_text[value_end:]
        return tag_text

    return _transform_html_contexts(
        html_text,
        replace_start_tag=replace_tag,
        replace_style_text=lambda css_text: _replace_css_asset_urls(
            css_text,
            lookup,
            max_output_chars=max_output_chars,
        ),
        max_output_chars=max_output_chars,
    )


def _asset_data_uri_lookup(data_uri_by_rel: dict[str, str]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for rel, data_uri in data_uri_by_rel.items():
        for variant in _asset_reference_variants(rel):
            normalized = _normalized_local_asset_reference_path(variant)
            if normalized is not None:
                lookup[normalized] = data_uri
    return lookup


def _normalized_local_asset_reference_path(raw_value: str) -> str | None:
    value = html.unescape(str(raw_value)).strip().strip("\"'")
    if not value or _is_external_or_data_url(value):
        return None
    parsed = _safe_urlparse(value)
    if parsed is None:
        return None
    path = urllib.parse.unquote(parsed.path).replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    if not path or path.startswith(("/", "../")) or "/../" in path:
        return None
    return path


def _replace_asset_url(value: str, lookup: dict[str, str]) -> str | None:
    leading_length = len(value) - len(value.lstrip())
    trailing_length = len(value) - len(value.rstrip())
    core_end = len(value) - trailing_length if trailing_length else len(value)
    core = value[leading_length:core_end]
    normalized = _normalized_local_asset_reference_path(core)
    if normalized is None:
        return None
    replacement = lookup.get(normalized)
    if replacement is None:
        return None
    fragment = urllib.parse.urlsplit(html.unescape(core)).fragment
    if fragment:
        replacement = f"{replacement}#{fragment}"
    return f"{value[:leading_length]}{replacement}{value[core_end:]}"


def _replace_srcset_asset_urls(
    value: str,
    lookup: dict[str, str],
    *,
    max_output_chars: int | None = None,
) -> str:
    replacements: list[tuple[int, int, str]] = []
    for value_start, value_end in _srcset_url_spans(value):
        replacement = _replace_asset_url(value[value_start:value_end], lookup)
        if replacement is not None:
            replacements.append((value_start, value_end, replacement))
    _require_bounded_replacement_size(
        value,
        replacements,
        max_output_chars=max_output_chars,
    )
    for value_start, value_end, replacement in reversed(replacements):
        value = value[:value_start] + replacement + value[value_end:]
    return value


def _srcset_url_spans(value: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(value):
        while index < len(value) and (value[index].isspace() or value[index] == ","):
            index += 1
        if index >= len(value):
            break
        value_start = index
        while index < len(value) and not value[index].isspace():
            index += 1
        token_end = index
        value_end = token_end
        while value_end > value_start and value[value_end - 1] == ",":
            value_end -= 1
        if value_end > value_start:
            spans.append((value_start, value_end))
        if value_end < token_end:
            continue
        parenthesis_depth = 0
        while index < len(value):
            character = value[index]
            if character == "(":
                parenthesis_depth += 1
            elif character == ")" and parenthesis_depth:
                parenthesis_depth -= 1
            elif character == "," and parenthesis_depth == 0:
                index += 1
                break
            index += 1
    return spans


def _html_attachment_resource_refs(html_text: str) -> list[str]:
    refs: list[str] = []

    def inspect_tag(tag_text: str, tag_name: str) -> str:
        identity = _html_tag_identity(tag_text)
        if identity is None:
            return tag_text
        resource_attributes = _HTML_RESOURCE_ATTRIBUTES.get(tag_name, frozenset())
        for attribute_name, value_start, value_end in _html_attribute_spans(
            tag_text,
            name_end=identity[2],
        ):
            value = html.unescape(tag_text[value_start:value_end]).strip()
            if attribute_name == "style":
                refs.extend(_css_resource_references(value))
            elif (
                attribute_name in _HTML_SRCSET_ATTRIBUTES
                and attribute_name in resource_attributes
            ):
                refs.extend(_srcset_resource_urls(value))
            elif attribute_name in resource_attributes:
                refs.append(value)
        return tag_text

    def inspect_style(css_text: str) -> str:
        refs.extend(_css_resource_references(css_text))
        return css_text

    _transform_html_contexts(
        html_text,
        replace_start_tag=inspect_tag,
        replace_style_text=inspect_style,
    )
    return refs


def _srcset_resource_urls(value: str) -> list[str]:
    source = html.unescape(value).strip()
    return [source[start:end] for start, end in _srcset_url_spans(source)]


def _css_resource_references(css_text: str) -> list[str]:
    refs: list[str] = []
    index = 0
    while index < len(css_text):
        if css_text.startswith("/*", index):
            index = _css_comment_end(css_text, index)
            continue
        if css_text[index] in {'"', "'"}:
            index = _css_string_end(css_text, index)
            continue
        import_end = _css_import_end(css_text, index)
        if import_end is not None:
            import_parts = _css_import_parts(css_text[index:import_end])
            if import_parts is not None:
                refs.append(import_parts[0])
            index = import_end
            continue
        url_bounds = _css_url_bounds(css_text, index)
        if url_bounds is not None:
            open_parenthesis, close_parenthesis = url_bounds
            refs.append(_css_url_value(css_text, open_parenthesis, close_parenthesis))
            index = close_parenthesis + 1
            continue
        index += 1
    return refs


def _replace_css_asset_urls(
    css_text: str,
    lookup: dict[str, str],
    *,
    max_output_chars: int | None = None,
) -> str:
    def replace(raw_url: str) -> str | None:
        replacement = _replace_asset_url(raw_url, lookup)
        return f"url({replacement})" if replacement is not None else None

    return _rewrite_css_urls(
        css_text,
        replace,
        max_output_chars=max_output_chars,
    )


def _rewrite_css_import_rules(
    css_text: str,
    replace_import: Callable[[str, str], str | None],
    *,
    max_output_chars: int | None = None,
) -> str:
    pieces: list[str] = []
    cursor = 0
    index = 0
    while index < len(css_text):
        if css_text.startswith("/*", index):
            index = _css_comment_end(css_text, index)
            continue
        if css_text[index] in {'"', "'"}:
            index = _css_string_end(css_text, index)
            continue
        import_end = _css_import_end(css_text, index)
        if import_end is None:
            index += 1
            continue
        import_parts = _css_import_parts(css_text[index:import_end])
        replacement = (
            replace_import(*import_parts) if import_parts is not None else None
        )
        if replacement is not None:
            pieces.append(css_text[cursor:index])
            pieces.append(replacement)
            cursor = import_end
        index = import_end
    if not pieces:
        return css_text
    pieces.append(css_text[cursor:])
    return _bounded_text_join(
        pieces,
        max_output_chars=max_output_chars,
    )


def _rewrite_css_urls(
    css_text: str,
    replace_url: Callable[[str], str | None],
    *,
    max_output_chars: int | None = None,
) -> str:
    pieces: list[str] = []
    cursor = 0
    index = 0
    while index < len(css_text):
        if css_text.startswith("/*", index):
            index = _css_comment_end(css_text, index)
            continue
        if css_text[index] in {'"', "'"}:
            index = _css_string_end(css_text, index)
            continue
        url_bounds = _css_url_bounds(css_text, index)
        if url_bounds is None:
            index += 1
            continue
        open_parenthesis, close_parenthesis = url_bounds
        replacement = replace_url(
            _css_url_value(css_text, open_parenthesis, close_parenthesis)
        )
        if replacement is not None:
            pieces.append(css_text[cursor:index])
            pieces.append(replacement)
            cursor = close_parenthesis + 1
        index = close_parenthesis + 1
    if not pieces:
        return css_text
    pieces.append(css_text[cursor:])
    return _bounded_text_join(
        pieces,
        max_output_chars=max_output_chars,
    )


def _css_import_parts(statement: str) -> tuple[str, str] | None:
    value = statement.strip()
    if not value.endswith(";"):
        return None
    if not value.startswith("@"):
        return None
    keyword = _css_identifier_at(value, 1)
    if keyword is None or keyword[0].casefold() != "import":
        return None
    index = _css_skip_whitespace_and_comments(value, keyword[1])
    url_bounds = _css_url_bounds(value, index) if index < len(value) else None
    if url_bounds is not None and url_bounds[0] == index:
        open_parenthesis, close_parenthesis = url_bounds
        raw_url = _css_url_value(value, open_parenthesis, close_parenthesis)
        value_end = close_parenthesis + 1
    elif index < len(value) and value[index] in {'"', "'"}:
        quote = value[index]
        value_end = _css_string_end(value, index)
        if value_end <= index + 1 or value[value_end - 1] != quote:
            return None
        raw_url = _css_unescape(value[index + 1 : value_end - 1])
    else:
        value_start = index
        while index < len(value) and (
            not value[index].isspace() and value[index] != ";"
        ):
            index += 1
        value_end = index
        raw_url = _css_unescape(value[value_start:value_end])
    if not raw_url.strip():
        return None
    return raw_url.strip(), value[value_end:-1]


def _css_skip_whitespace_and_comments(css_text: str, start: int) -> int:
    index = start
    while index < len(css_text):
        if css_text[index].isspace():
            index += 1
            continue
        if css_text.startswith("/*", index):
            index = _css_comment_end(css_text, index)
            continue
        break
    return index


def _css_url_value(
    css_text: str,
    open_parenthesis: int,
    close_parenthesis: int,
) -> str:
    value = css_text[open_parenthesis + 1 : close_parenthesis].strip()
    if len(value) >= 2 and value[0] in {'"', "'"} and value[-1] == value[0]:
        value = value[1:-1]
    return _css_unescape(value)


def _css_comment_end(css_text: str, start: int) -> int:
    end = css_text.find("*/", start + 2)
    return len(css_text) if end < 0 else end + 2


def _css_string_end(css_text: str, start: int) -> int:
    quote = css_text[start]
    index = start + 1
    while index < len(css_text):
        if css_text[index] == "\\":
            index += 2
            continue
        if css_text[index] == quote:
            return index + 1
        index += 1
    return len(css_text)


def _css_escape_at(css_text: str, start: int) -> tuple[str, int] | None:
    if start >= len(css_text) or css_text[start] != "\\":
        return None
    index = start + 1
    if index >= len(css_text):
        return None
    current = css_text[index]
    if current == "\r":
        index += 1
        if index < len(css_text) and css_text[index] == "\n":
            index += 1
        return "", index
    if current in {"\n", "\f"}:
        return "", index + 1
    if current.casefold() in "0123456789abcdef":
        value_start = index
        while (
            index < len(css_text)
            and index - value_start < 6
            and css_text[index].casefold() in "0123456789abcdef"
        ):
            index += 1
        codepoint = int(css_text[value_start:index], 16)
        if index < len(css_text) and css_text[index].isspace():
            if css_text[index] == "\r" and css_text[index : index + 2] == "\r\n":
                index += 2
            else:
                index += 1
        if codepoint == 0 or codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
            return "\ufffd", index
        return chr(codepoint), index
    return current, index + 1


def _css_unescape(value: str) -> str:
    pieces: list[str] = []
    index = 0
    while index < len(value):
        if value[index] != "\\":
            pieces.append(value[index])
            index += 1
            continue
        escaped = _css_escape_at(value, index)
        if escaped is None:
            pieces.append("\\")
            index += 1
            continue
        decoded, index = escaped
        pieces.append(decoded)
    return "".join(pieces)


def _css_identifier_at(css_text: str, start: int) -> tuple[str, int] | None:
    if start > 0:
        previous = css_text[start - 1]
        if (
            previous.isalnum()
            or previous in "_-"
            or previous == "\\"
            or ord(previous) >= 0x80
        ):
            return None
    pieces: list[str] = []
    index = start
    while index < len(css_text):
        current = css_text[index]
        if current == "\\":
            escaped = _css_escape_at(css_text, index)
            if escaped is None or not escaped[0]:
                break
            pieces.append(escaped[0])
            index = escaped[1]
            continue
        if current.isalnum() or current in "_-" or ord(current) >= 0x80:
            pieces.append(current)
            index += 1
            continue
        break
    if not pieces:
        return None
    return "".join(pieces), index


def _css_url_bounds(css_text: str, start: int) -> tuple[int, int] | None:
    identifier = _css_identifier_at(css_text, start)
    if identifier is None or identifier[0].casefold() != "url":
        return None
    index = identifier[1]
    index = _css_skip_whitespace_and_comments(css_text, index)
    if index >= len(css_text) or css_text[index] != "(":
        return None
    close_parenthesis = _css_parenthesis_end(css_text, index)
    if close_parenthesis is None:
        return None
    return index, close_parenthesis


def _css_parenthesis_end(css_text: str, open_parenthesis: int) -> int | None:
    depth = 1
    index = open_parenthesis + 1
    while index < len(css_text):
        if css_text.startswith("/*", index):
            index = _css_comment_end(css_text, index)
            continue
        if css_text[index] in {'"', "'"}:
            index = _css_string_end(css_text, index)
            continue
        if css_text[index] == "(":
            depth += 1
        elif css_text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _css_import_end(css_text: str, start: int) -> int | None:
    if start >= len(css_text) or css_text[start] != "@":
        return None
    keyword = _css_identifier_at(css_text, start + 1)
    if keyword is None or keyword[0].casefold() != "import":
        return None
    after_keyword = keyword[1]
    depth = 0
    index = after_keyword
    while index < len(css_text):
        if css_text.startswith("/*", index):
            index = _css_comment_end(css_text, index)
            continue
        if css_text[index] in {'"', "'"}:
            index = _css_string_end(css_text, index)
            continue
        if css_text[index] == "(":
            depth += 1
        elif css_text[index] == ")" and depth:
            depth -= 1
        elif css_text[index] == ";" and depth == 0:
            return index + 1
        index += 1
    return None


def _replace_stylesheet_links_with_style_tags(
    html_text: str,
    css_text_by_rel: dict[str, str],
    *,
    max_output_chars: int | None = None,
) -> str:
    if not css_text_by_rel:
        return html_text

    def replace(tag_text: str, tag_name: str) -> str:
        if tag_name != "link":
            return tag_text
        identity = _html_tag_identity(tag_text)
        if identity is None:
            return tag_text
        attributes = {
            name: html.unescape(tag_text[value_start:value_end]).strip()
            for name, value_start, value_end in _html_attribute_spans(
                tag_text,
                name_end=identity[2],
            )
        }
        rel_tokens = {token.casefold() for token in attributes.get("rel", "").split()}
        if (
            "stylesheet" not in rel_tokens
            or "alternate" in rel_tokens
            or "disabled" in attributes
        ):
            return tag_text
        stylesheet_type = attributes.get("type", "").strip().casefold()
        if stylesheet_type and stylesheet_type != "text/css":
            return tag_text
        href = attributes.get("href", "")
        css_text = css_text_by_rel.get(href) or css_text_by_rel.get(
            urllib.parse.unquote(href)
        )
        if css_text is None:
            css_text = _css_text_for_reference(href, css_text_by_rel)
        if css_text is None:
            return tag_text
        style_attributes = "".join(
            f' {name}="{html.escape(value, quote=True)}"'
            for name in ("media", "title")
            if (value := attributes.get(name, "").strip())
        )
        safe_css = _css_text_for_style_tag(css_text)
        return f"<style{style_attributes}>\n{safe_css}\n</style>"

    return _transform_html_contexts(
        html_text,
        replace_start_tag=replace,
        max_output_chars=max_output_chars,
    )


def _replace_style_imports_with_embedded_css(
    html_text: str,
    css_text_by_rel: dict[str, str],
    *,
    max_output_chars: int | None = None,
) -> str:
    if not css_text_by_rel:
        return html_text

    def replace(css_source: str) -> str:
        def replace_import(raw_url: str, tail: str) -> str | None:
            css_text = _css_text_for_reference(raw_url, css_text_by_rel)
            if css_text is None:
                return None
            return _css_import_replacement(css_text, tail)

        return _rewrite_css_import_rules(
            css_source,
            replace_import,
            max_output_chars=max_output_chars,
        )

    return _transform_html_contexts(
        html_text,
        replace_style_text=replace,
        max_output_chars=max_output_chars,
    )


def _css_with_embedded_local_assets(
    css_path: Path,
    *,
    assets_dir: Path,
    budget: _LocalAssetEmbeddingBudget,
    seen: set[Path] | None = None,
    max_output_chars: int | None = None,
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

    def replace_import(raw_url: str, tail: str) -> str | None:
        if not raw_url or _is_external_or_data_url(raw_url):
            return None
        target = _local_asset_target(css_path.parent, raw_url, assets_dir=assets_dir)
        if target is None:
            missing.append(raw_url)
            return None
        if not target.is_file():
            missing.append(raw_url)
            return None
        imported_css, imported_missing = _css_with_embedded_local_assets(
            target,
            assets_dir=assets_dir,
            budget=budget,
            seen=seen,
            max_output_chars=max_output_chars,
        )
        missing.extend(imported_missing)
        if imported_css is None:
            return None
        return _css_import_replacement(imported_css, tail)

    def replace_url(raw_url: str) -> str | None:
        if not raw_url or _is_external_or_data_url(raw_url):
            return None
        target = _local_asset_target(css_path.parent, raw_url, assets_dir=assets_dir)
        if target is None:
            missing.append(raw_url)
            return None
        if not target.is_file():
            missing.append(raw_url)
            return None
        data_uri = budget.data_uri(target)
        if data_uri is None:
            missing.append(raw_url)
            return None
        return f'url("{data_uri}")'

    css_text = _rewrite_css_import_rules(
        css_text,
        replace_import,
        max_output_chars=max_output_chars,
    )
    css_text = _rewrite_css_urls(
        css_text,
        replace_url,
        max_output_chars=max_output_chars,
    )
    seen.discard(resolved_css_path)
    return css_text, missing


def _css_import_replacement(css_text: str, tail: str) -> str | None:
    css_text = _css_text_for_style_tag(css_text)
    qualifiers = _css_import_qualifiers(tail)
    if qualifiers is None:
        return None
    layer_name, supports_condition, media_query = qualifiers
    if media_query:
        css_text = f"@media {media_query} {{\n{css_text}\n}}"
    if supports_condition is not None:
        condition = _css_supports_rule_condition(supports_condition)
        css_text = f"@supports {condition} {{\n{css_text}\n}}"
    if layer_name is not None:
        layer_prefix = f" {layer_name}" if layer_name else ""
        css_text = f"@layer{layer_prefix} {{\n{css_text}\n}}"
    return css_text


def _css_import_qualifiers(
    tail: str,
) -> tuple[str | None, str | None, str] | None:
    value = tail.strip()
    index = _css_skip_whitespace_and_comments(value, 0)
    layer_name: str | None = None
    if _css_keyword_at(value, index, "layer"):
        after_keyword = index + len("layer")
        next_index = _css_skip_whitespace_and_comments(value, after_keyword)
        if next_index < len(value) and value[next_index] == "(":
            close_parenthesis = _css_parenthesis_end(value, next_index)
            if close_parenthesis is None:
                return None
            layer_name = value[next_index + 1 : close_parenthesis].strip()
            if not layer_name:
                return None
            index = close_parenthesis + 1
        else:
            layer_name = ""
            index = after_keyword

    index = _css_skip_whitespace_and_comments(value, index)
    supports_condition: str | None = None
    if _css_keyword_at(value, index, "supports"):
        after_keyword = index + len("supports")
        function_start = _css_skip_whitespace_and_comments(value, after_keyword)
        if function_start >= len(value) or value[function_start] != "(":
            return None
        close_parenthesis = _css_parenthesis_end(value, function_start)
        if close_parenthesis is None:
            return None
        supports_condition = value[function_start + 1 : close_parenthesis].strip()
        if not supports_condition:
            return None
        index = close_parenthesis + 1

    index = _css_skip_whitespace_and_comments(value, index)
    return layer_name, supports_condition, value[index:].strip()


def _css_keyword_at(value: str, index: int, keyword: str) -> bool:
    end = index + len(keyword)
    if value[index:end].casefold() != keyword:
        return False
    return end >= len(value) or not (value[end].isalnum() or value[end] in "_-")


def _css_supports_rule_condition(value: str) -> str:
    condition = value.strip()
    lowered = condition.casefold()
    if condition.startswith("(") or lowered.startswith(
        ("not ", "selector(", "font-tech(", "font-format(")
    ):
        return condition
    return f"({condition})"


def _css_text_for_style_tag(css_text: str) -> str:
    css_text = re.sub(r"^\s*@charset\s+[^;]+;\s*", "", css_text, flags=re.IGNORECASE)
    return re.sub(
        r"</(?=style\b)", lambda _match: r"<\/", css_text, flags=re.IGNORECASE
    )


def _css_text_for_reference(
    raw_url: str, css_text_by_rel: dict[str, str]
) -> str | None:
    value = html.unescape(raw_url).strip().strip("\"'")
    if _is_external_or_data_url(value):
        return None
    parsed = _safe_urlparse(value)
    if parsed is None:
        return None
    parsed_path = parsed.path
    decoded_path = urllib.parse.unquote(parsed_path)
    variants: list[str] = []
    for candidate in (value, parsed_path, decoded_path):
        variants.extend(_asset_reference_variants(candidate))
        if candidate.startswith("./"):
            variants.extend(_asset_reference_variants(candidate[2:]))
    for variant in dict.fromkeys(variants):
        css_text = css_text_by_rel.get(variant)
        if css_text is not None:
            return css_text
    return None


def _local_asset_target(
    base_dir: Path, raw_url: str, *, assets_dir: Path
) -> Path | None:
    parsed = _safe_urlparse(raw_url)
    if parsed is None:
        return None
    local_path = urllib.parse.unquote(parsed.path)
    if not local_path:
        return None
    try:
        target = (base_dir / local_path).resolve()
        assets_root = assets_dir.resolve()
        target.relative_to(assets_root)
    except (OSError, RuntimeError, ValueError):
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
    for scanned, path in enumerate(assets_dir.rglob("*")):
        if scanned >= limit:
            truncated = True
            break
        if not path.is_file() and not path.is_symlink():
            continue
        files.append(path)
    files.sort(key=lambda path: str(path).casefold())
    return files, truncated


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
        self.root = assets_dir.resolve(strict=True)
        self.max_asset_bytes = max(max_asset_bytes, 0)
        self.max_total_bytes = max(max_total_bytes, 0)
        self.max_assets = max(max_assets, 0)
        self.total_bytes = 0
        self.skipped: list[dict[str, str]] = []
        self._skipped_keys: set[tuple[str, str]] = set()
        self._reserved: dict[Path, int] = {}
        self._data_uris: dict[Path, str] = {}
        self._css_text: dict[Path, str] = {}
        self._fingerprints: dict[Path, _FileFingerprint] = {}
        self._lexical_paths: dict[Path, Path] = {}

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
        except (OSError, RuntimeError, ValueError):
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
        try:
            snapshot = _read_file_snapshot_bounded(
                resolved,
                max_bytes=self.max_asset_bytes,
            )
        except OSError:
            self.record_skip(path, "asset_unstable_or_unreadable")
            return None
        payload = snapshot.payload
        if self.total_bytes + len(payload) > self.max_total_bytes:
            self.record_skip(path, "asset_total_bytes_limit")
            return None
        self._reserved[resolved] = len(payload)
        self._fingerprints[resolved] = snapshot.fingerprint
        self._lexical_paths[resolved] = path
        self.total_bytes += len(payload)
        return payload

    def changed_input_path(self) -> Path | None:
        for resolved, expected in self._fingerprints.items():
            lexical = self._lexical_paths[resolved]
            if lexical.is_symlink():
                return lexical
            try:
                if lexical.resolve(strict=True) != resolved:
                    return lexical
            except (OSError, RuntimeError):
                return lexical
            if not _path_fingerprint_matches(
                resolved,
                expected,
                max_bytes=self.max_asset_bytes,
            ):
                return lexical
        return None


def _data_uri(payload: bytes, mime_type: str) -> str:
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _is_css_asset(path: Path) -> bool:
    return path.suffix.casefold() == ".css"


def _safe_urlparse(value: str) -> urllib.parse.ParseResult | None:
    try:
        return urllib.parse.urlparse(value)
    except (TypeError, ValueError):
        return None


def _is_external_or_data_url(url: str) -> bool:
    value = url.strip()
    parsed = _safe_urlparse(value)
    if parsed is None:
        return False
    scheme = parsed.scheme.casefold()
    if scheme == "file" or re.match(r"^[A-Za-z]:[\\/]", value):
        return False
    return bool(scheme or parsed.netloc) or value.casefold().startswith(("data:", "#"))


def _download_needs_ocr(item: dict[str, Any]) -> bool:
    identity = item.get("identity")
    if item.get("status") == "downloaded_needs_ocr":
        return True
    return isinstance(identity, dict) and identity.get("needs_ocr") is True


def _inventory_probe_attachment_key(inventory: dict[str, object]) -> str | None:
    attachments = inventory.get("attachments")
    if not isinstance(attachments, list):
        return None
    for wanted in (
        "application/pdf",
        "text/html",
        "application/xhtml+xml",
        "multipart/related",
        "message/rfc822",
    ):
        for item in attachments:
            if not isinstance(item, dict):
                continue
            key = _validated_zotero_attachment_key(item.get("key"))
            content_type = str(item.get("content_type") or "").casefold()
            if key and content_type == wanted:
                return key
    for item in attachments:
        if not isinstance(item, dict):
            continue
        key = _validated_zotero_attachment_key(item.get("key"))
        if key:
            return key
    return None


def _validated_zotero_attachment_key(value: object) -> str:
    if not isinstance(value, str):
        return ""
    key = value.strip()
    return key if re.fullmatch(r"[A-Za-z0-9]{1,64}", key) is not None else ""


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
    for field in (
        "newAttachmentKey",
        "attachmentKey",
        "siblingKey",
    ):
        value = relay_result.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        return _validated_zotero_attachment_key(value)
    return ""


def _validated_attachment_storage_dir(storage_dir: Path, key: str) -> Path:
    if storage_dir.is_symlink():
        raise OSError(f"Attachment storage root is a symlink: {storage_dir}")
    storage_dir.mkdir(parents=True, exist_ok=True)
    if storage_dir.is_symlink() or not storage_dir.is_dir():
        raise OSError(f"Attachment storage root is invalid: {storage_dir}")
    try:
        storage_root = storage_dir.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise OSError(f"Attachment storage root is unstable: {storage_dir}") from exc

    target_dir = storage_dir / key
    if target_dir.is_symlink():
        raise OSError(f"Attachment storage directory is a symlink: {target_dir}")
    target_dir.mkdir(exist_ok=True)
    if target_dir.is_symlink() or not target_dir.is_dir():
        raise OSError(f"Attachment storage directory is invalid: {target_dir}")
    try:
        resolved = target_dir.resolve(strict=True)
        resolved.relative_to(storage_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise OSError(
            f"Attachment storage directory escapes storage root: {target_dir}"
        ) from exc
    return resolved


def _safe_filename(value: str) -> str:
    return safe_filename_component(value, default="document", max_chars=180)
