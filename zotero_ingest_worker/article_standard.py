from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import stat
import tempfile
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from zoteropdf2md.html_contract import canonical_contract_report
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
ARTICLE_PACKAGE_MAX_OUTPUT_BYTES = 128_000_000
ARTICLE_PACKAGE_MAX_QUALITY_BYTES = 1_000_000
ARTICLE_PACKAGE_MAX_PATH_PARTS = 64
ARTICLE_PACKAGE_MAX_TREE_ENTRIES = (
    ARTICLE_PACKAGE_MAX_ASSETS * ARTICLE_PACKAGE_MAX_PATH_PARTS
    + ARTICLE_PACKAGE_MAX_SCANNED_ASSETS
    + 64
)


@dataclass(frozen=True)
class _FileFingerprint:
    bytes: int
    sha256: str
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class _BoundedFileCopy:
    source: _FileFingerprint
    target_device: int
    target_inode: int


@dataclass(frozen=True)
class _FileBytesSnapshot:
    payload: bytes
    fingerprint: _FileFingerprint


@dataclass(frozen=True)
class _JsonObjectSnapshot:
    value: dict[str, Any]
    fingerprint: _FileFingerprint


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
    source_fingerprint = _stable_file_fingerprint(
        source_html,
        max_bytes=ARTICLE_PACKAGE_MAX_SOURCE_BYTES,
    )
    source_url = str(
        source_download.get("final_url") or source_download.get("url") or ""
    )
    try:
        polished = polish_web_html_file(source_html, source_url=source_url or None)
    except (OSError, WebHtmlPolishError) as exc:
        return {
            "ok": False,
            "reason": "source_html_polish_failed",
            "source_path": str(source_html),
            "error": f"{exc.__class__.__name__}: {exc}"[:500],
        }
    _require_source_html_unchanged(source_html, expected=source_fingerprint)
    if package_dir.is_symlink():
        return {
            "ok": False,
            "reason": "article_package_symlink",
            "source_path": str(source_html),
        }
    package_dir.parent.mkdir(parents=True, exist_ok=True)
    _recover_interrupted_article_package(package_dir)
    staging_dir = Path(
        tempfile.mkdtemp(
            prefix=f".{package_dir.name}.staging-",
            dir=package_dir.parent,
        )
    )
    try:
        (staging_dir / "source").mkdir()
        (staging_dir / "logs").mkdir()

        article_html = staging_dir / ARTICLE_HTML_FILENAME
        source_copy = staging_dir / "source" / source_html.name
        polish = _web_polish_manifest(polished)
        html_text, assets = _article_html_with_standard_assets(
            source_html=source_html,
            package_dir=staging_dir,
            html_text=polished.html,
        )
        article_fingerprint = _write_text_file_bounded(
            article_html,
            html_text,
            max_bytes=ARTICLE_PACKAGE_MAX_OUTPUT_BYTES,
        )
        del html_text, polished
        copied_source_fingerprint = _copy_file_bounded(
            source_html,
            source_copy,
            max_bytes=ARTICLE_PACKAGE_MAX_SOURCE_BYTES,
        )
        if copied_source_fingerprint != source_fingerprint:
            raise OSError(
                "Article package source changed or exceeded its limit during copy"
            )
        _require_source_html_unchanged(source_html, expected=source_fingerprint)
        source_copy_fingerprint = _stable_file_fingerprint(
            source_copy,
            max_bytes=ARTICLE_PACKAGE_MAX_SOURCE_BYTES,
        )
        if not _same_file_content(
            source_copy_fingerprint,
            source_fingerprint,
        ):
            raise OSError("Article package source copy does not match source HTML")

        quality = evaluate_article_html(
            article_html=article_html,
            metadata=metadata,
            source_download=source_download,
            article_verdict=article_verdict or {},
        )
        evaluated_fingerprint = _stable_file_fingerprint(
            article_html,
            max_bytes=ARTICLE_PACKAGE_MAX_OUTPUT_BYTES,
        )
        if evaluated_fingerprint != article_fingerprint:
            raise OSError("Article HTML changed while evaluating package quality")
        accepted = quality["status"] in {"passed", "warning"}
        if not accepted:
            return {
                "ok": False,
                "reason": "article_quality_failed",
                "standard": ARTICLE_HTML_STANDARD_VERSION,
                "normalizer": NATIVE_HTML_NORMALIZER_VERSION,
                "package_dir": str(package_dir),
                "quality_status": quality["status"],
                "quality_failures": quality["failures"],
                "polish": polish,
                "previous_package_retained": package_dir.is_dir(),
            }

        quality_path = staging_dir / "quality.json"
        _write_text_file_bounded(
            quality_path,
            json.dumps(quality, ensure_ascii=False, indent=2),
            max_bytes=ARTICLE_PACKAGE_MAX_QUALITY_BYTES,
        )
        integrity = _article_package_integrity_manifest(
            staging_dir=staging_dir,
            article_html=article_html,
            source_copy=source_copy,
            quality_path=quality_path,
            assets=assets,
        )
        article_integrity = integrity["article_html"]
        if (
            article_integrity["bytes"] != article_fingerprint.bytes
            or article_integrity["sha256"] != article_fingerprint.sha256
        ):
            raise OSError("Article HTML changed before package integrity sealing")
        manifest = build_article_manifest(
            article_html=article_html,
            metadata=metadata,
            source_download=source_download,
            source_context=source_context,
            source_fingerprint=source_fingerprint,
            quality=quality,
            assets=assets,
            polish=polish,
            integrity=integrity,
        )
        manifest_path = staging_dir / "manifest.json"
        _write_text_file_bounded(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2),
            max_bytes=ARTICLE_PACKAGE_MAX_QUALITY_BYTES,
        )
        _require_source_html_unchanged(source_html, expected=source_fingerprint)
        backup_cleanup_warning = _promote_article_package_tree(staging_dir, package_dir)
        result: dict[str, Any] = {
            "ok": True,
            "standard": ARTICLE_HTML_STANDARD_VERSION,
            "normalizer": NATIVE_HTML_NORMALIZER_VERSION,
            "package_dir": str(package_dir),
            "article_html_path": str(package_dir / ARTICLE_HTML_FILENAME),
            "manifest_path": str(package_dir / "manifest.json"),
            "quality_path": str(package_dir / "quality.json"),
            "quality_status": quality["status"],
            "quality_failures": quality["failures"],
            "polish": polish,
            "source_sha256": source_fingerprint.sha256,
            "article_sha256": integrity["article_html"]["sha256"],
        }
        if backup_cleanup_warning is not None:
            result["backup_cleanup_warning"] = backup_cleanup_warning
        return result
    finally:
        if staging_dir.exists() or staging_dir.is_symlink():
            _remove_article_package_path(staging_dir)


def _path_device_inode(path: Path) -> tuple[int, int] | None:
    try:
        if path.is_symlink():
            return None
        current = path.stat()
    except (OSError, RuntimeError):
        return None
    return int(current.st_dev), int(current.st_ino)


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _stable_file_fingerprint(path: Path, *, max_bytes: int | None) -> _FileFingerprint:
    if path.is_symlink() or not path.is_file():
        raise OSError(f"Expected a regular file without symlinks: {path}")
    limit = max(max_bytes, 0) if max_bytes is not None else None
    before = path.stat()
    expected_bytes = int(before.st_size)
    if expected_bytes < 0:
        raise OSError(f"File has an invalid negative size: {path}")
    if limit is not None and expected_bytes > limit:
        raise OSError(f"File exceeds {limit} bytes: {path}")
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        opened_before = os.fstat(stream.fileno())
        while total < expected_bytes:
            read_size = min(1_048_576, expected_bytes - total)
            chunk = stream.read(read_size)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
        growth_probe = stream.read(1)
        if growth_probe:
            total += len(growth_probe)
            digest.update(growth_probe)
        opened_after = os.fstat(stream.fileno())
    if path.is_symlink():
        raise OSError(f"File became a symlink while fingerprinting: {path}")
    after = path.stat()
    if not _stat_observations_match(
        before=before,
        opened_before=opened_before,
        opened_after=opened_after,
        after=after,
        observed_bytes=total,
    ):
        raise OSError(f"File changed while fingerprinting: {path}")
    return _FileFingerprint(
        bytes=total,
        sha256=digest.hexdigest(),
        device=int(after.st_dev),
        inode=int(after.st_ino),
        mtime_ns=int(after.st_mtime_ns),
        ctime_ns=int(after.st_ctime_ns),
    )


def _read_file_snapshot_bounded(path: Path, *, max_bytes: int) -> _FileBytesSnapshot:
    if path.is_symlink() or not path.is_file():
        raise OSError(f"Expected a regular file without symlinks: {path}")
    limit = max(max_bytes, 0)
    before = path.stat()
    if int(before.st_size) > limit:
        raise OSError(f"File exceeds {limit} bytes: {path}")
    with path.open("rb") as stream:
        opened_before = os.fstat(stream.fileno())
        payload = stream.read(limit + 1)
        opened_after = os.fstat(stream.fileno())
    if len(payload) > limit:
        raise OSError(f"File exceeds {limit} bytes: {path}")
    if path.is_symlink():
        raise OSError(f"File became a symlink while reading: {path}")
    after = path.stat()
    size = len(payload)
    if not _stat_observations_match(
        before=before,
        opened_before=opened_before,
        opened_after=opened_after,
        after=after,
        observed_bytes=size,
    ):
        raise OSError(f"File changed while reading: {path}")
    return _FileBytesSnapshot(
        payload=payload,
        fingerprint=_FileFingerprint(
            bytes=size,
            sha256=hashlib.sha256(payload).hexdigest(),
            device=int(after.st_dev),
            inode=int(after.st_ino),
            mtime_ns=int(after.st_mtime_ns),
            ctime_ns=int(after.st_ctime_ns),
        ),
    )


def _unlink_owned_regular_file(
    path: Path,
    *,
    device: int,
    inode: int,
) -> None:
    try:
        current = path.lstat()
        if not stat.S_ISREG(current.st_mode):
            return
        if (int(current.st_dev), int(current.st_ino)) != (device, inode):
            return
        path.unlink()
    except OSError:
        # Cleanup must not replace the write failure that triggered it.
        return


def _write_text_file_bounded(
    path: Path,
    text: str,
    *,
    max_bytes: int,
) -> _FileFingerprint:
    limit = max(max_bytes, 0)
    if len(text) > limit:
        raise OSError(f"Text exceeds {limit} bytes when encoded as UTF-8: {path}")
    digest = hashlib.sha256()
    created_identity: tuple[int, int] | None = None
    total = 0
    try:
        with path.open("xb") as stream:
            opened = os.fstat(stream.fileno())
            created_identity = (int(opened.st_dev), int(opened.st_ino))
            for offset in range(0, len(text), 262_144):
                payload = text[offset : offset + 262_144].encode("utf-8")
                total += len(payload)
                if total > limit:
                    raise OSError(
                        f"Text exceeds {limit} bytes when encoded as UTF-8: {path}"
                    )
                stream.write(payload)
                digest.update(payload)
            stream.flush()
            os.fsync(stream.fileno())
        current = _stable_file_fingerprint(path, max_bytes=limit)
        if current.bytes != total or current.sha256 != digest.hexdigest():
            raise OSError(f"File changed after writing: {path}")
        return current
    except BaseException as exc:
        if created_identity is not None:
            _unlink_owned_regular_file(
                path, device=created_identity[0], inode=created_identity[1]
            )
        if isinstance(exc, UnicodeError):
            raise OSError(f"Text cannot be encoded as UTF-8: {path}") from exc
        raise


def _require_source_html_unchanged(
    source_html: Path,
    *,
    expected: _FileFingerprint,
) -> None:
    try:
        current = _stable_file_fingerprint(
            source_html,
            max_bytes=ARTICLE_PACKAGE_MAX_SOURCE_BYTES,
        )
    except OSError as exc:
        raise OSError(
            f"source HTML changed during article package build: {source_html}"
        ) from exc
    if current != expected:
        raise OSError(
            f"source HTML changed during article package build: {source_html}"
        )


def _same_file_content(left: _FileFingerprint, right: _FileFingerprint) -> bool:
    return left.bytes == right.bytes and left.sha256 == right.sha256


def _integrity_record(
    path: Path,
    *,
    relative_path: str,
    max_bytes: int,
) -> dict[str, Any]:
    fingerprint = _stable_file_fingerprint(path, max_bytes=max_bytes)
    return {
        "path": relative_path,
        "bytes": fingerprint.bytes,
        "sha256": fingerprint.sha256,
    }


def _article_package_integrity_manifest(
    *,
    staging_dir: Path,
    article_html: Path,
    source_copy: Path,
    quality_path: Path,
    assets: list[dict[str, Any]],
) -> dict[str, Any]:
    asset_integrity: list[dict[str, Any]] = []
    for asset in assets:
        if asset.get("status") != "copied":
            continue
        relative_path = str(asset.get("path") or "")
        asset_path = _contained_package_file(staging_dir, relative_path)
        if asset_path is None:
            raise OSError(f"Invalid copied article asset path: {relative_path}")
        record = _integrity_record(
            asset_path,
            relative_path=relative_path,
            max_bytes=ARTICLE_PACKAGE_MAX_ASSET_BYTES,
        )
        if int(asset.get("bytes") or -1) != record["bytes"]:
            raise OSError(f"Copied article asset byte count changed: {relative_path}")
        if str(asset.get("sha256") or "") != record["sha256"]:
            raise OSError(f"Copied article asset digest changed: {relative_path}")
        asset_integrity.append(record)
    asset_integrity.sort(key=lambda record: str(record["path"]).casefold())
    return {
        "article_html": _integrity_record(
            article_html,
            relative_path=ARTICLE_HTML_FILENAME,
            max_bytes=ARTICLE_PACKAGE_MAX_OUTPUT_BYTES,
        ),
        "source_copy": _integrity_record(
            source_copy,
            relative_path=f"source/{source_copy.name}",
            max_bytes=ARTICLE_PACKAGE_MAX_SOURCE_BYTES,
        ),
        "quality": _integrity_record(
            quality_path,
            relative_path="quality.json",
            max_bytes=ARTICLE_PACKAGE_MAX_QUALITY_BYTES,
        ),
        "assets": asset_integrity,
    }


def _promote_article_package_tree(staging_dir: Path, package_dir: Path) -> str | None:
    if not _article_package_tree_complete(staging_dir):
        raise OSError(f"Article package staging tree is incomplete: {staging_dir}")
    published_owner = _path_device_inode(staging_dir)
    if published_owner is None:
        raise OSError(f"Article package staging ownership is unstable: {staging_dir}")
    backup_dir: Path | None = None
    backup_owner: tuple[int, int] | None = None
    published_by_this_call = False
    try:
        if package_dir.exists() or package_dir.is_symlink():
            if package_dir.is_symlink():
                raise OSError(f"Article package target is a symlink: {package_dir}")
            backup_owner = _path_device_inode(package_dir)
            if backup_owner is None:
                raise OSError(
                    f"Article package target ownership is unstable: {package_dir}"
                )
            backup_dir = package_dir.with_name(
                f".{package_dir.name}.backup-{uuid.uuid4().hex}"
            )
            os.replace(package_dir, backup_dir)
            if _path_device_inode(backup_dir) != backup_owner:
                raise OSError(f"Article package backup ownership changed: {backup_dir}")
        os.replace(staging_dir, package_dir)
        published_by_this_call = True
        if _path_device_inode(package_dir) != published_owner:
            raise OSError(f"Published article package ownership changed: {package_dir}")
        if not _article_package_tree_complete(package_dir):
            raise OSError(
                f"Published article package tree is incomplete: {package_dir}"
            )
        if _path_device_inode(package_dir) != published_owner:
            raise OSError(f"Published article package ownership changed: {package_dir}")
    except BaseException as exc:
        recovery_errors: list[BaseException] = []
        if (
            published_by_this_call
            and _path_device_inode(package_dir) == published_owner
        ):
            try:
                _remove_article_package_path(package_dir)
            except BaseException as recovery_exc:
                recovery_errors.append(recovery_exc)
        if (
            backup_dir is not None
            and backup_owner is not None
            and (backup_dir.exists() or backup_dir.is_symlink())
            and not package_dir.exists()
            and not package_dir.is_symlink()
        ):
            if _path_device_inode(backup_dir) == backup_owner:
                try:
                    os.replace(backup_dir, package_dir)
                    if _path_device_inode(package_dir) != backup_owner:
                        raise OSError(
                            f"Restored article package ownership changed: {package_dir}"
                        )
                except BaseException as recovery_exc:
                    recovery_errors.append(recovery_exc)
        elif (
            backup_dir is not None
            and backup_owner is not None
            and _path_device_inode(backup_dir) == backup_owner
        ):
            try:
                _remove_article_package_path(backup_dir)
            except BaseException as recovery_exc:
                recovery_errors.append(recovery_exc)
        for recovery_error in recovery_errors:
            exc.add_note(
                "Article package rollback error: "
                f"{type(recovery_error).__name__}: {recovery_error}"
            )
        raise
    else:
        if backup_dir is not None and (backup_dir.exists() or backup_dir.is_symlink()):
            if backup_owner is None or _path_device_inode(backup_dir) != backup_owner:
                return (f"Article package backup ownership changed: {backup_dir}")[:500]
            try:
                _remove_article_package_path(backup_dir)
            except OSError as exc:
                return f"{exc.__class__.__name__}: {exc}"[:500]
        return None


def _recover_interrupted_article_package(package_dir: Path) -> None:
    backup_paths = sorted(
        package_dir.parent.glob(f".{package_dir.name}.backup-*"),
        key=_path_mtime_ns,
        reverse=True,
    )
    backups = [
        (backup, owner)
        for backup in backup_paths
        if (owner := _path_device_inode(backup)) is not None
    ]
    if not backups:
        return
    package_present = package_dir.exists() or package_dir.is_symlink()
    package_owner = _path_device_inode(package_dir)
    if package_present and package_owner is None:
        raise OSError(f"Article package recovery target is unstable: {package_dir}")
    if _article_package_tree_complete(package_dir):
        for backup, owner in backups:
            try:
                _remove_owned_article_package_path(backup, owner=owner)
            except OSError:
                pass
        return
    for backup, backup_owner in backups:
        if not _article_package_tree_complete(backup):
            continue
        if package_present:
            if _path_device_inode(package_dir) != package_owner:
                return
        elif package_dir.exists() or package_dir.is_symlink():
            return
        if _path_device_inode(backup) != backup_owner:
            continue
        if package_present and package_owner is not None:
            if not _remove_owned_article_package_path(
                package_dir,
                owner=package_owner,
            ):
                return
        if package_dir.exists() or package_dir.is_symlink():
            return
        if _path_device_inode(backup) != backup_owner:
            return
        os.replace(backup, package_dir)
        if _path_device_inode(package_dir) != backup_owner:
            raise OSError(f"Recovered article package ownership changed: {package_dir}")
        for stale_backup, stale_owner in backups:
            if stale_backup == backup:
                continue
            _remove_owned_article_package_path(
                stale_backup,
                owner=stale_owner,
            )
        return


def _article_package_tree_complete(package_dir: Path) -> bool:
    if not package_dir.is_dir() or package_dir.is_symlink():
        return False
    manifest_path = package_dir / "manifest.json"
    quality_path = package_dir / "quality.json"
    manifest_snapshot = _read_json_object_bounded(
        manifest_path,
        max_bytes=ARTICLE_PACKAGE_MAX_QUALITY_BYTES,
    )
    quality_snapshot = _read_json_object_bounded(
        quality_path,
        max_bytes=ARTICLE_PACKAGE_MAX_QUALITY_BYTES,
    )
    if manifest_snapshot is None or quality_snapshot is None:
        return False
    manifest = manifest_snapshot.value
    quality = quality_snapshot.value
    if quality.get("status") not in {"passed", "warning"}:
        return False
    manifest_quality = manifest.get("quality")
    if not isinstance(manifest_quality, dict) or manifest_quality.get(
        "status"
    ) != quality.get("status"):
        return False
    if not _article_package_integrity_valid(
        package_dir=package_dir,
        manifest=manifest,
        quality=quality,
        quality_fingerprint=quality_snapshot.fingerprint,
    ):
        return False
    try:
        current_manifest = _stable_file_fingerprint(
            manifest_path,
            max_bytes=ARTICLE_PACKAGE_MAX_QUALITY_BYTES,
        )
        current_quality = _stable_file_fingerprint(
            quality_path,
            max_bytes=ARTICLE_PACKAGE_MAX_QUALITY_BYTES,
        )
    except OSError:
        return False
    return (
        current_manifest == manifest_snapshot.fingerprint
        and current_quality == quality_snapshot.fingerprint
    )


def validated_article_package_html_path(article_html: Path) -> Path | None:
    if article_html.name != ARTICLE_HTML_FILENAME or article_html.is_symlink():
        return None
    if not _article_package_tree_complete(article_html.parent):
        return None
    return article_html


def _article_package_integrity_valid(
    *,
    package_dir: Path,
    manifest: dict[str, Any],
    quality: dict[str, Any],
    quality_fingerprint: _FileFingerprint,
) -> bool:
    if manifest.get("schema_version") != 2:
        return False
    if manifest.get("standard") != ARTICLE_HTML_STANDARD_VERSION:
        return False
    if quality.get("standard") != ARTICLE_HTML_STANDARD_VERSION:
        return False
    normalizer = manifest.get("normalizer")
    if (
        not isinstance(normalizer, dict)
        or normalizer.get("version") != NATIVE_HTML_NORMALIZER_VERSION
    ):
        return False
    if manifest.get("files") != {
        "article_html": ARTICLE_HTML_FILENAME,
        "assets_dir": ASSETS_DIRNAME,
        "manifest": "manifest.json",
        "quality": "quality.json",
        "source_dir": "source",
        "logs_dir": "logs",
    }:
        return False
    integrity = manifest.get("integrity")
    if not isinstance(integrity, dict):
        return False
    fixed_records = (
        ("article_html", ARTICLE_HTML_FILENAME, ARTICLE_PACKAGE_MAX_OUTPUT_BYTES),
        ("quality", "quality.json", ARTICLE_PACKAGE_MAX_QUALITY_BYTES),
    )
    expected_paths = {"manifest.json"}
    for key, expected_path, max_bytes in fixed_records:
        record = integrity.get(key)
        if not _integrity_record_matches(
            package_dir=package_dir,
            record=record,
            expected_path=expected_path,
            max_bytes=max_bytes,
            actual_fingerprint=(quality_fingerprint if key == "quality" else None),
        ):
            return False
        expected_paths.add(expected_path)

    source_record = integrity.get("source_copy")
    if not isinstance(source_record, dict):
        return False
    source_path = str(source_record.get("path") or "")
    if not source_path.startswith("source/") or source_path.count("/") != 1:
        return False
    if not _integrity_record_matches(
        package_dir=package_dir,
        record=source_record,
        expected_path=source_path,
        max_bytes=ARTICLE_PACKAGE_MAX_SOURCE_BYTES,
    ):
        return False
    source_metadata = manifest.get("source")
    if not isinstance(source_metadata, dict):
        return False
    if source_metadata.get("bytes") != source_record.get("bytes"):
        return False
    if source_metadata.get("sha256") != source_record.get("sha256"):
        return False
    manifest_quality = manifest.get("quality")
    if not isinstance(manifest_quality, dict):
        return False
    if manifest_quality.get("failures") != quality.get("failures"):
        return False
    if manifest_quality.get("warnings") != quality.get("warnings"):
        return False
    if manifest.get("canonical") != quality.get("canonical_contract"):
        return False
    expected_paths.add(source_path)

    asset_records = integrity.get("assets")
    if (
        not isinstance(asset_records, list)
        or len(asset_records) > ARTICLE_PACKAGE_MAX_ASSETS
    ):
        return False
    copied_assets = manifest.get("assets")
    if (
        not isinstance(copied_assets, list)
        or len(copied_assets) > ARTICLE_PACKAGE_MAX_SCANNED_ASSETS + 1
    ):
        return False
    asset_items: list[dict[str, Any]] = []
    for item in copied_assets:
        if not isinstance(item, dict):
            return False
        status = item.get("status")
        path = item.get("path")
        byte_count = item.get("bytes")
        if (
            status not in {"copied", "skipped"}
            or not isinstance(path, str)
            or not path
            or len(path) > 4096
            or isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or byte_count < 0
        ):
            return False
        if status == "copied" and not re.fullmatch(
            r"[0-9a-f]{64}",
            str(item.get("sha256") or ""),
        ):
            return False
        if status == "skipped" and not str(item.get("reason") or ""):
            return False
        asset_items.append(item)
    copied_records = [item for item in asset_items if item.get("status") == "copied"]
    copied_by_path = {str(item.get("path") or ""): item for item in copied_records}
    if len(copied_by_path) != len(copied_records) or len(copied_by_path) != len(
        asset_records
    ):
        return False
    seen_assets: set[str] = set()
    for record in asset_records:
        if not isinstance(record, dict):
            return False
        relative_path = str(record.get("path") or "")
        if (
            not relative_path.startswith(f"{ASSETS_DIRNAME}/")
            or relative_path in seen_assets
            or relative_path not in copied_by_path
        ):
            return False
        copied_record = copied_by_path[relative_path]
        if copied_record.get("bytes") != record.get("bytes"):
            return False
        if copied_record.get("sha256") != record.get("sha256"):
            return False
        if not _integrity_record_matches(
            package_dir=package_dir,
            record=record,
            expected_path=relative_path,
            max_bytes=ARTICLE_PACKAGE_MAX_ASSET_BYTES,
        ):
            return False
        seen_assets.add(relative_path)
        expected_paths.add(relative_path)

    actual_paths = _article_package_payload_paths(package_dir)
    return actual_paths == expected_paths


def _integrity_record_matches(
    *,
    package_dir: Path,
    record: object,
    expected_path: str,
    max_bytes: int,
    actual_fingerprint: _FileFingerprint | None = None,
) -> bool:
    if not isinstance(record, dict):
        return False
    if str(record.get("path") or "") != expected_path:
        return False
    byte_count = record.get("bytes")
    digest = str(record.get("sha256") or "")
    if (
        isinstance(byte_count, bool)
        or not isinstance(byte_count, int)
        or byte_count < 0
    ):
        return False
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        return False
    path = _contained_package_file(package_dir, expected_path)
    if path is None:
        return False
    if actual_fingerprint is None:
        try:
            actual_fingerprint = _stable_file_fingerprint(path, max_bytes=max_bytes)
        except OSError:
            return False
    return (
        actual_fingerprint.bytes == byte_count and actual_fingerprint.sha256 == digest
    )


def _contained_package_file(package_dir: Path, relative_path: str) -> Path | None:
    if not relative_path or len(relative_path) > 4096 or "\\" in relative_path:
        return None
    relative = Path(relative_path)
    if relative.is_absolute() or relative.drive:
        return None
    parts = relative.parts
    if (
        not parts
        or len(parts) > ARTICLE_PACKAGE_MAX_PATH_PARTS
        or any(part in {"", ".", ".."} or ":" in part for part in parts)
    ):
        return None
    candidate = package_dir.joinpath(*parts)
    current = package_dir
    for part in parts:
        current = current / part
        if current.is_symlink():
            return None
    try:
        package_root = package_dir.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(package_root)
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None


def _article_package_payload_paths(package_dir: Path) -> set[str] | None:
    paths: set[str] = set()
    scanned = 0
    try:
        for path in package_dir.rglob("*"):
            scanned += 1
            if scanned > ARTICLE_PACKAGE_MAX_TREE_ENTRIES:
                return None
            relative = path.relative_to(package_dir).as_posix()
            if (
                len(path.relative_to(package_dir).parts)
                > ARTICLE_PACKAGE_MAX_PATH_PARTS
            ):
                return None
            if relative == "logs" or relative.startswith("logs/"):
                if path.is_symlink():
                    return None
                continue
            if path.is_symlink():
                return None
            if path.is_file():
                paths.add(relative)
            elif not path.is_dir():
                return None
    except OSError:
        return None
    return paths


def _read_json_object_bounded(
    path: Path,
    *,
    max_bytes: int,
) -> _JsonObjectSnapshot | None:
    if path.is_symlink() or not path.is_file():
        return None
    limit = max(max_bytes, 0)
    try:
        before = path.stat()
        with path.open("rb") as stream:
            opened_before = os.fstat(stream.fileno())
            payload = stream.read(limit + 1)
            opened_after = os.fstat(stream.fileno())
        if len(payload) > limit:
            return None
        if path.is_symlink():
            return None
        after = path.stat()
        size = len(payload)
        if not _stat_observations_match(
            before=before,
            opened_before=opened_before,
            opened_after=opened_after,
            after=after,
            observed_bytes=size,
        ):
            return None
        parsed = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return _JsonObjectSnapshot(
        value=parsed,
        fingerprint=_FileFingerprint(
            bytes=size,
            sha256=hashlib.sha256(payload).hexdigest(),
            device=int(after.st_dev),
            inode=int(after.st_ino),
            mtime_ns=int(after.st_mtime_ns),
            ctime_ns=int(after.st_ctime_ns),
        ),
    )


def _remove_article_package_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _remove_owned_article_package_path(
    path: Path,
    *,
    owner: tuple[int, int],
) -> bool:
    if _path_device_inode(path) != owner:
        return False
    claim_path = path.with_name(f".{path.name}.remove-claim-{uuid.uuid4().hex}")
    try:
        os.rename(path, claim_path)
    except FileNotFoundError:
        return False
    except OSError:
        if _path_device_inode(path) != owner:
            return False
        raise
    if _path_device_inode(claim_path) != owner:
        _restore_unowned_article_package_claim(
            claim_path,
            original_path=path,
        )
        return False
    try:
        _remove_article_package_path(claim_path)
    except BaseException as exc:
        if not _restore_owned_article_package_claim(
            claim_path,
            original_path=path,
            owner=owner,
        ):
            exc.add_note(
                "Owned article package claim could not be restored; "
                f"inspect claim={claim_path} and original={path}"
            )
        raise
    if claim_path.exists() or claim_path.is_symlink():
        removal_error = OSError(
            f"Article package removal did not remove its owned claim: {claim_path}"
        )
        if not _restore_owned_article_package_claim(
            claim_path,
            original_path=path,
            owner=owner,
        ):
            removal_error.add_note(
                "Owned article package claim could not be restored; "
                f"inspect claim={claim_path} and original={path}"
            )
        raise removal_error
    return True


def _restore_owned_article_package_claim(
    claim_path: Path,
    *,
    original_path: Path,
    owner: tuple[int, int],
) -> bool:
    if _path_device_inode(claim_path) != owner:
        return False
    if original_path.exists() or original_path.is_symlink():
        return False
    try:
        os.rename(claim_path, original_path)
    except OSError:
        return False
    return _path_device_inode(original_path) == owner


def _restore_unowned_article_package_claim(
    claim_path: Path,
    *,
    original_path: Path,
) -> None:
    if not (claim_path.exists() or claim_path.is_symlink()):
        return
    if original_path.exists() or original_path.is_symlink():
        return
    try:
        os.rename(claim_path, original_path)
    except OSError:
        return


def _path_mtime_ns(path: Path) -> int:
    try:
        return int(path.stat().st_mtime_ns)
    except OSError:
        return 0


def build_article_manifest(
    *,
    article_html: Path,
    metadata: Any,
    source_download: dict[str, Any],
    source_context: str,
    source_fingerprint: _FileFingerprint,
    quality: dict[str, Any],
    assets: list[dict[str, Any]],
    polish: dict[str, Any],
    integrity: dict[str, Any],
) -> dict[str, Any]:
    identifiers = _metadata_identifiers(metadata)
    return {
        "schema_version": 2,
        "standard": ARTICLE_HTML_STANDARD_VERSION,
        "canonical": quality["canonical_contract"],
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
            "bytes": source_fingerprint.bytes,
            "sha256": source_fingerprint.sha256,
        },
        "files": {
            "article_html": article_html.name,
            "assets_dir": ASSETS_DIRNAME,
            "manifest": "manifest.json",
            "quality": "quality.json",
            "source_dir": "source",
            "logs_dir": "logs",
        },
        "integrity": integrity,
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
    max_bytes: int = ARTICLE_PACKAGE_MAX_OUTPUT_BYTES,
) -> dict[str, Any]:
    snapshot = _read_file_snapshot_bounded(article_html, max_bytes=max_bytes)
    payload = snapshot.payload
    snapshot_fingerprint = snapshot.fingerprint
    del snapshot
    text = payload.decode("utf-8", errors="replace")
    del payload
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
        re.findall(r"<style\b(?![^>]*\bdata-z2m-style\s*=)", text, re.IGNORECASE)
    )
    active_media_count = len(
        re.findall(r"<(?:audio|video|iframe|object|embed)\b", text, re.IGNORECASE)
    )
    unsafe_attribute_count = _unsafe_attribute_count(text)
    math = _math_strategy(text)
    canonical_contract = canonical_contract_report(text)

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
    if canonical_contract["status"] == "failed":
        failures.append("canonical_contract_failed")
    if missing_internal_links:
        warnings.append("missing_internal_link_targets")
    if canonical_contract["status"] == "warning":
        warnings.append("canonical_contract_warning")
    status = "failed" if failures else "warning" if warnings else "passed"
    quality = {
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
        "canonical_contract": canonical_contract,
        "article_verdict": article_verdict,
        "failures": failures,
        "warnings": warnings,
    }
    current_fingerprint = _stable_file_fingerprint(
        article_html,
        max_bytes=max_bytes,
    )
    if current_fingerprint != snapshot_fingerprint:
        raise OSError(f"Article HTML changed while evaluating quality: {article_html}")
    return quality


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
        source_snapshot = _read_file_snapshot_bounded(
            source_html,
            max_bytes=ARTICLE_PACKAGE_MAX_SOURCE_BYTES,
        )
        html_text = source_snapshot.payload.decode("utf-8", errors="replace")
        del source_snapshot
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
            assets.append(
                _skipped_asset(relative.as_posix(), "asset_too_large", size=size)
            )
            continue
        if copied_count >= max(max_assets, 0):
            assets.append(
                _skipped_asset(relative.as_posix(), "asset_count_limit", size=size)
            )
            continue
        if total_bytes + size > max(max_total_asset_bytes, 0):
            assets.append(
                _skipped_asset(
                    relative.as_posix(), "asset_total_bytes_limit", size=size
                )
            )
            continue
        target = target_assets_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        copied_fingerprint = _copy_file_bounded(
            resolved,
            target,
            max_bytes=min(
                max(max_asset_bytes, 0), max(max_total_asset_bytes, 0) - total_bytes
            ),
        )
        if copied_fingerprint is None:
            assets.append(
                _skipped_asset(
                    relative.as_posix(), "asset_changed_or_too_large", size=size
                )
            )
            continue
        copied_count += 1
        total_bytes += copied_fingerprint.bytes
        assets.append(
            {
                "path": f"{ASSETS_DIRNAME}/{relative.as_posix()}",
                "bytes": copied_fingerprint.bytes,
                "sha256": copied_fingerprint.sha256,
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


def _copy_file_bounded(
    source: Path,
    target: Path,
    *,
    max_bytes: int | None,
) -> _FileFingerprint | None:
    publication = _copy_file_bounded_with_owner(
        source,
        target,
        max_bytes=max_bytes,
    )
    return publication.source if publication is not None else None


def _copy_file_bounded_with_owner(
    source: Path,
    target: Path,
    *,
    max_bytes: int | None,
) -> _BoundedFileCopy | None:
    limit = max(max_bytes, 0) if max_bytes is not None else None
    temp = target.with_name(f".{target.name}.article-asset-tmp-{uuid.uuid4().hex}")
    copied = 0
    created_identity: tuple[int, int] | None = None
    try:
        if source.is_symlink() or not source.is_file():
            return None
        before = source.stat()
        expected_bytes = int(before.st_size)
        if expected_bytes < 0:
            return None
        if limit is not None and expected_bytes > limit:
            return None
        digest = hashlib.sha256()
        with source.open("rb") as source_stream, temp.open("xb") as target_stream:
            opened_before = os.fstat(source_stream.fileno())
            target_opened = os.fstat(target_stream.fileno())
            created_identity = (
                int(target_opened.st_dev),
                int(target_opened.st_ino),
            )
            while copied < expected_bytes:
                chunk = source_stream.read(min(1_048_576, expected_bytes - copied))
                if not chunk:
                    break
                copied += len(chunk)
                target_stream.write(chunk)
                digest.update(chunk)
            if source_stream.read(1):
                return None
            target_stream.flush()
            os.fsync(target_stream.fileno())
            opened_after = os.fstat(source_stream.fileno())
        if source.is_symlink():
            return None
        after = source.stat()
        if not _stat_observations_match(
            before=before,
            opened_before=opened_before,
            opened_after=opened_after,
            after=after,
            observed_bytes=copied,
        ):
            return None
        copied_fingerprint = _FileFingerprint(
            bytes=copied,
            sha256=digest.hexdigest(),
            device=int(after.st_dev),
            inode=int(after.st_ino),
            mtime_ns=int(after.st_mtime_ns),
            ctime_ns=int(after.st_ctime_ns),
        )
        current_source = _stable_file_fingerprint(source, max_bytes=limit)
        if current_source != copied_fingerprint:
            return None
        temp_fingerprint = _stable_file_fingerprint(temp, max_bytes=limit)
        if not _same_file_content(temp_fingerprint, copied_fingerprint):
            return None
        if created_identity is None:
            raise OSError(f"Bounded copy did not capture target ownership: {target}")
        publication = _BoundedFileCopy(
            source=copied_fingerprint,
            target_device=created_identity[0],
            target_inode=created_identity[1],
        )
        os.replace(temp, target)
        return publication
    except BaseException:
        if created_identity is not None:
            _unlink_owned_regular_file(
                target,
                device=created_identity[0],
                inode=created_identity[1],
            )
        raise
    finally:
        if created_identity is not None:
            _unlink_owned_regular_file(
                temp,
                device=created_identity[0],
                inode=created_identity[1],
            )


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
    text = re.sub(
        r"<(script|style)\b[^>]*>.*?</\1>", " ", text, flags=re.IGNORECASE | re.DOTALL
    )
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _html_title(text: str) -> str:
    match = re.search(
        r"<title[^>]*>(.*?)</title>", text, flags=re.IGNORECASE | re.DOTALL
    )
    return _visible_text(match.group(1)) if match else ""


def _html_attr_values(text: str, tag: str, attr: str) -> list[str]:
    pattern = re.compile(
        rf"<{tag}\b[^>]*\b{attr}\s*=\s*(['\"])(.*?)\1",
        flags=re.IGNORECASE | re.DOTALL,
    )
    return [
        html.unescape(match.group(2)).strip()
        for match in pattern.finditer(text)
        if match.group(2).strip()
    ]


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
        if not value or lowered.startswith(
            ("http://", "https://", "//", "data:", "javascript:", "mailto:", "#")
        ):
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
    return {
        html.unescape(match.group(2)).strip()
        for match in pattern.finditer(text)
        if match.group(2).strip()
    }


def urllib_fragment(value: str) -> str:
    return html.unescape(value[1:]).strip()


def _bibliography_anchor_count(anchors: set[str]) -> int:
    return sum(
        1
        for anchor in anchors
        if re.search(r"(ref|bib|reference)", anchor, flags=re.IGNORECASE)
    )


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
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_part(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return value.strip("._-")[:80] or "article"


def _stat_cross_api_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


def _stat_observations_match(
    *,
    before: os.stat_result,
    opened_before: os.stat_result,
    opened_after: os.stat_result,
    after: os.stat_result,
    observed_bytes: int,
) -> bool:
    return (
        _stat_identity(before) == _stat_identity(after)
        and _stat_identity(opened_before) == _stat_identity(opened_after)
        and _stat_cross_api_identity(before) == _stat_cross_api_identity(opened_before)
        and _stat_cross_api_identity(after) == _stat_cross_api_identity(opened_after)
        and observed_bytes == int(before.st_size)
        and observed_bytes == int(after.st_size)
    )
