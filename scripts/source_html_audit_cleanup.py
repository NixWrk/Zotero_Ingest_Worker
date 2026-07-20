from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
from typing import Any, Callable, cast
import signal
import shutil
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import scripts.bulk_repolish_source_html as bulk  # noqa: E402
from zotero_ingest_worker.bounded_io import read_text_bounded  # noqa: E402
from zotero_ingest_worker.source_html_quality_audit import run_audit  # noqa: E402


TEX_DOCKER_IMAGES = (
    "ghcr.io/xu-cheng/texlive-full:latest",
    "danteev/texlive:latest",
)
DEFAULT_REPOLISH_TIMEOUT_SECONDS = 6 * 60 * 60
SUBPROCESS_TAIL_BYTES = 4096
MAX_RELAY_RESPONSE_BYTES = 1024 * 1024
MAX_RELAY_ERROR_BYTES = 64 * 1024
MAX_AUDIT_JSON_BYTES = 64 * 1024 * 1024


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Clean source HTML audit leftovers: orphan storage folders, stale [ARXIV HTML] "
            "attachments that have SOURCE HTML siblings, and LaTeXML figure records needing repolish."
        )
    )
    parser.add_argument("--audit-json", type=Path, default=None)
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--delete-webdav", action="store_true")
    parser.add_argument("--skip-repolish", action="store_true")
    parser.add_argument("--skip-remote-arxiv-check", action="store_true")
    parser.add_argument("--request-timeout", type=_positive_int_arg, default=300)
    parser.add_argument(
        "--repolish-timeout-seconds",
        type=_positive_int_arg,
        default=DEFAULT_REPOLISH_TIMEOUT_SECONDS,
    )
    args = parser.parse_args(argv)

    dry_run = not args.apply
    if args.apply and not args.confirm:
        raise SystemExit("--apply requires --confirm.")

    run_root = _run_root(args.output_root)
    run_root.mkdir(parents=True, exist_ok=True)
    manifest_path = run_root / "manifest.json"
    results_path = run_root / "results.jsonl"
    manifest: dict[str, Any] = {
        "ok": None,
        "run_root": str(run_root),
        "dry_run": dry_run,
        "delete_webdav": bool(args.delete_webdav),
        "started_at": _utc_now(),
    }
    _write_json(manifest_path, manifest)

    report = _load_or_run_audit(args.audit_json, run_root=run_root)
    plan = cleanup_plan_from_audit(report)
    relay = None
    relay_bindings: list[Any] = []
    remote_check_ok = True
    if not args.skip_remote_arxiv_check:
        try:
            relay = bulk._relay_env()
            relay_bindings = bulk._relay_bindings(relay)
            remote_records = find_remote_stale_arxiv_html_records(
                report,
                relay=relay,
                bindings=relay_bindings,
                timeout=args.request_timeout,
            )
            if remote_records:
                plan["stale_arxiv_html"] = _unique_records(
                    [*plan["stale_arxiv_html"], *remote_records]
                )
            manifest["remote_stale_arxiv_check"] = {
                "ok": True,
                "added": len(remote_records),
            }
        except Exception as exc:
            remote_check_ok = False
            manifest["remote_stale_arxiv_check"] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
    else:
        manifest["remote_stale_arxiv_check"] = {
            "ok": True,
            "skipped": True,
            "reason": "operator_requested",
        }
    _write_json(run_root / "cleanup_plan.json", plan)
    manifest["plan_counts"] = {name: len(items) for name, items in plan.items()}
    _write_json(manifest_path, manifest)

    if not remote_check_ok:
        manifest.update(
            {
                "ok": False,
                "aborted_reason": "remote_stale_arxiv_check_failed",
                "finished_at": _utc_now(),
                "results_path": str(results_path),
            }
        )
        _write_json(manifest_path, manifest)
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 1

    if dry_run:
        manifest.update(
            {"ok": True, "finished_at": _utc_now(), "results_path": str(results_path)}
        )
        _write_json(manifest_path, manifest)
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0

    ok = True
    sqlite_backups: dict[Path, dict[str, Any]] = {}
    if plan["latexml_repolish"] and not args.skip_repolish:
        repolish = run_targeted_repolish(
            keys=[str(record["key"]) for record in plan["latexml_repolish"]],
            output_root=run_root / "repolish",
            dry_run=False,
            request_timeout=args.request_timeout,
            timeout_seconds=args.repolish_timeout_seconds,
        )
        _append_jsonl(results_path, {"action": "targeted_repolish", **repolish})
        ok = ok and repolish.get("ok") is True

    if plan["stale_arxiv_html"] and relay is None:
        relay = bulk._relay_env()
        relay_bindings = bulk._relay_bindings(relay)
    for record in plan["stale_arxiv_html"]:
        item: dict[str, Any] = {
            "action": "trash_stale_arxiv_html",
            "record": _compact_record(record),
        }
        try:
            relay_record = dict(record)
            relay_record["library_id"] = relay_library_id_for_record(
                record, relay_bindings
            )
            relay_result = trash_stale_arxiv_html(
                relay_record,
                relay=relay or {},
                dry_run=False,
                delete_webdav=args.delete_webdav,
                timeout=args.request_timeout,
                deduplication_prefix=f"source-html-audit-cleanup:{run_root.name}",
            )
            item["relay"] = relay_result
            if relay_result.get("ok") is True:
                binding = relay_binding_for_record(record, relay_bindings)
                if binding is not None:
                    sqlite_path = Path(binding.host_data_dir) / "zotero.sqlite"
                    if sqlite_path not in sqlite_backups:
                        sqlite_backups[sqlite_path] = bulk._sqlite_backup(
                            sqlite_path,
                            run_root
                            / "sqlite"
                            / f"{_safe_name(str(binding.library_id))}.sqlite",
                        )
                        manifest["sqlite_backups"] = list(sqlite_backups.values())
                        _write_json(manifest_path, manifest)
                    item["local_deleted"] = mark_local_attachment_deleted(
                        record,
                        binding=binding,
                        relay_result=relay_result,
                    )
                if record.get("remote_only"):
                    item["local_quarantine"] = {
                        "ok": True,
                        "skipped": True,
                        "reason": "remote_only_attachment",
                    }
                else:
                    item["local_quarantine"] = quarantine_storage_dir(
                        record,
                        run_root=run_root,
                        dry_run=False,
                        label="stale_arxiv_html",
                    )
        except Exception as exc:
            item["ok"] = False
            item["error"] = f"{type(exc).__name__}: {exc}"
            ok = False
        else:
            result_names = (
                "relay",
                "local_deleted",
                "local_quarantine",
            )
            item["ok"] = all(
                isinstance(item.get(name), dict) and item[name].get("ok") is True
                for name in result_names
                if name in item
            )
            ok = ok and item["ok"] is True
        _append_jsonl(results_path, item)

    for record in plan["orphan_source_html"]:
        item = {
            "action": "quarantine_orphan_source_html",
            "record": _compact_record(record),
        }
        try:
            item["local_quarantine"] = quarantine_storage_dir(
                record,
                run_root=run_root,
                dry_run=False,
                label="orphan_source_html",
            )
            item["ok"] = item["local_quarantine"].get("ok") is True
            ok = ok and item["ok"] is True
        except Exception as exc:
            item["ok"] = False
            item["error"] = f"{type(exc).__name__}: {exc}"
            ok = False
        _append_jsonl(results_path, item)

    for record in plan["orphan_arxiv_html"]:
        item = {
            "action": "quarantine_orphan_arxiv_html",
            "record": _compact_record(record),
        }
        try:
            item["local_quarantine"] = quarantine_storage_dir(
                record,
                run_root=run_root,
                dry_run=False,
                label="orphan_arxiv_html",
            )
            item["ok"] = item["local_quarantine"].get("ok") is True
            ok = ok and item["ok"] is True
        except Exception as exc:
            item["ok"] = False
            item["error"] = f"{type(exc).__name__}: {exc}"
            ok = False
        _append_jsonl(results_path, item)

    manifest.update(
        {"ok": ok, "finished_at": _utc_now(), "results_path": str(results_path)}
    )
    _write_json(manifest_path, manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def cleanup_plan_from_audit(report: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    records = _unique_records(
        report.get("all_records") or report.get("critical_records") or []
    )
    return {
        "orphan_source_html": [
            record
            for record in records
            if record.get("is_source_html")
            and _has_issue(record, "missing_zotero_attachment_record")
        ],
        "orphan_arxiv_html": [
            record
            for record in records
            if record.get("is_arxiv_html")
            and _has_issue(record, "missing_zotero_attachment_record")
        ],
        "stale_arxiv_html": [
            record
            for record in records
            if record.get("is_arxiv_html")
            and _has_issue(record, "stale_arxiv_html_attachment")
        ],
        "latexml_repolish": [
            record
            for record in records
            if record.get("is_source_html")
            and not _has_issue(record, "missing_zotero_attachment_record")
            and any(
                _has_issue(record, issue)
                for issue in (
                    "latexml_figure_render_error",
                    "latexml_itemize_marker_layout",
                    "latexml_inline_black_text",
                    "latexml_math_black_color",
                    "missing_web_polish_style",
                    "missing_web_doc_main",
                    "missing_source_kind",
                    "missing_latexml_table_style",
                    "script_tags_present",
                    "absolute_fragment_links_resolve_local",
                )
            )
        ],
    }


def find_remote_stale_arxiv_html_records(
    report: dict[str, Any],
    *,
    relay: dict[str, str],
    bindings: list[Any],
    timeout: int,
) -> list[dict[str, Any]]:
    remote_by_library: dict[str, list[dict[str, Any]]] = {}
    source_parent_keys = source_parent_keys_by_relay_library(report, bindings)
    for library_id, parent_keys in source_parent_keys.items():
        if not parent_keys:
            continue
        children: list[dict[str, Any]] = []
        for parent_key in sorted(parent_keys):
            for child in list_remote_item_children(
                relay=relay,
                library_id=library_id,
                parent_key=parent_key,
                timeout=timeout,
            ):
                child.setdefault("parentItem", parent_key)
                children.append(child)
        remote_by_library[library_id] = children
    return remote_stale_arxiv_records(
        report,
        bindings=bindings,
        remote_by_library=remote_by_library,
    )


def remote_stale_arxiv_records(
    report: dict[str, Any],
    *,
    bindings: list[Any],
    remote_by_library: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    source_parent_keys = source_parent_keys_by_relay_library(report, bindings)
    existing_stale_keys = {
        (
            relay_library_id_for_record(record, bindings),
            str(record.get("key") or "").strip(),
        )
        for record in _unique_records(
            report.get("all_records") or report.get("critical_records") or []
        )
        if _has_issue(record, "stale_arxiv_html_attachment")
        and str(record.get("key") or "").strip()
    }
    records: list[dict[str, Any]] = []
    for library_id, attachments in remote_by_library.items():
        source_parents = source_parent_keys.get(library_id) or set()
        if not source_parents:
            continue
        for attachment in attachments:
            key = str(attachment.get("key") or "").strip()
            parent_key = str(attachment.get("parentItem") or "").strip()
            if (
                not key
                or (library_id, key) in existing_stale_keys
                or parent_key not in source_parents
            ):
                continue
            if attachment.get("deleted"):
                continue
            if not _remote_attachment_looks_like_arxiv_html(attachment):
                continue
            records.append(
                {
                    "library_id": library_id,
                    "key": key,
                    "parent_key": parent_key,
                    "title": str(
                        attachment.get("title") or attachment.get("filename") or key
                    ),
                    "path": "",
                    "is_source_html": False,
                    "is_arxiv_html": True,
                    "remote_only": True,
                    "remote_version": attachment.get("version"),
                    "issues": [
                        "stale_arxiv_html_attachment",
                        "remote_only_arxiv_html_attachment",
                    ],
                }
            )
    return records


def source_parent_keys_by_relay_library(
    report: dict[str, Any],
    bindings: list[Any],
) -> dict[str, set[str]]:
    by_library: dict[str, set[str]] = {}
    records = _unique_records(
        report.get("all_records") or report.get("critical_records") or []
    )
    for record in records:
        if not record.get("is_source_html"):
            continue
        if _has_issue(record, "missing_zotero_attachment_record"):
            continue
        parent_key = str(record.get("parent_key") or "").strip()
        if not parent_key:
            continue
        library_id = relay_library_id_for_record(record, bindings)
        if not library_id:
            continue
        by_library.setdefault(library_id, set()).add(parent_key)
    return by_library


def list_remote_html_attachments(
    *,
    relay: dict[str, str],
    library_id: str,
    timeout: int,
) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"contentType": "text/html", "maxItems": "50000"})
    url = (
        f"{relay['url'].rstrip('/')}/libraries/"
        f"{urllib.parse.quote(library_id, safe='')}/remote/attachments?{query}"
    )
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {relay['token']}",
        },
        method="GET",
    )
    payload = _relay_json_request(request, timeout=timeout)
    return _validated_remote_items(payload, field="attachments")


def list_remote_item_children(
    *,
    relay: dict[str, str],
    library_id: str,
    parent_key: str,
    timeout: int,
) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"libraryId": library_id, "source": "web"})
    url = (
        f"{relay['url'].rstrip('/')}/items/"
        f"{urllib.parse.quote(parent_key, safe='')}/children?{query}"
    )
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {relay['token']}",
        },
        method="GET",
    )
    payload = _relay_json_request(request, timeout=timeout)
    return _validated_remote_items(payload, field="children")


def _validated_remote_items(
    payload: object,
    *,
    field: str,
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError(f"relay {field} invalid response contract: {payload!r}")
    items = payload.get(field)
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise RuntimeError(f"relay {field} invalid response contract: {payload!r}")
    return items


def _remote_attachment_looks_like_arxiv_html(attachment: dict[str, Any]) -> bool:
    haystack = " ".join(
        str(attachment.get(field) or "")
        for field in ("title", "filename", "contentType")
    ).casefold()
    return "[arxiv html]" in haystack and "text/html" in haystack


def quarantine_storage_dir(
    record: dict[str, Any],
    *,
    run_root: Path,
    dry_run: bool,
    label: str,
) -> dict[str, Any]:
    storage_dir = _storage_dir_for_record(record)
    target = _unique_path(
        run_root
        / "storage_quarantine"
        / label
        / _safe_name(str(record.get("library_id") or "unknown_library"))
        / storage_dir.name
    )
    if dry_run:
        return {
            "ok": True,
            "dryRun": True,
            "wouldMove": str(storage_dir),
            "target": str(target),
        }
    if not storage_dir.exists():
        return {
            "ok": True,
            "dryRun": False,
            "skipped": True,
            "reason": "storage_dir_missing",
            "path": str(storage_dir),
        }
    if storage_dir.is_symlink() or not storage_dir.is_dir():
        raise RuntimeError(f"refusing to quarantine non-directory path: {storage_dir}")
    source_owner = _path_device_inode(storage_dir)
    if source_owner is None:
        raise RuntimeError(
            f"could not establish storage directory ownership: {storage_dir}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    claim_path = storage_dir.with_name(
        f".{storage_dir.name}.quarantine-claim-{uuid.uuid4().hex}"
    )
    staging_path = target.with_name(
        f".{target.name}.quarantine-stage-{uuid.uuid4().hex}"
    )
    try:
        os.rename(storage_dir, claim_path)
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"storage directory disappeared before quarantine: {storage_dir}"
        ) from exc
    except OSError:
        if _path_device_inode(storage_dir) != source_owner:
            raise RuntimeError(
                f"storage directory ownership changed before quarantine: {storage_dir}"
            )
        raise
    if _path_device_inode(claim_path) != source_owner:
        _restore_unowned_quarantine_claim(
            claim_path,
            original_path=storage_dir,
        )
        raise RuntimeError(
            f"storage directory ownership changed while claiming: {storage_dir}"
        )

    staging_owner: tuple[int, int] | None = None
    published_owner: tuple[int, int] | None = None
    try:
        shutil.copytree(claim_path, staging_path, symlinks=True)
        staging_owner = _path_device_inode(staging_path)
        if staging_owner is None:
            raise OSError(
                f"quarantine staging directory has no stable owner: {staging_path}"
            )
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"quarantine target appeared during copy: {target}")
        os.rename(staging_path, target)
        published_owner = staging_owner
        if _path_device_inode(target) != published_owner:
            raise OSError(f"published quarantine target ownership changed: {target}")
    except BaseException as exc:
        cleanup_errors: list[BaseException] = []
        current_staging_owner = staging_owner or _path_device_inode(staging_path)
        if (
            current_staging_owner is not None
            and _path_device_inode(staging_path) == current_staging_owner
        ):
            try:
                _remove_quarantine_path(staging_path)
            except BaseException as cleanup_exc:
                cleanup_errors.append(cleanup_exc)
        if (
            published_owner is not None
            and _path_device_inode(target) == published_owner
        ):
            try:
                _remove_quarantine_path(target)
            except BaseException as cleanup_exc:
                cleanup_errors.append(cleanup_exc)
        if not _restore_owned_quarantine_claim(
            claim_path,
            original_path=storage_dir,
            owner=source_owner,
        ):
            exc.add_note(
                "Owned quarantine claim could not be restored; "
                f"inspect claim={claim_path} and original={storage_dir}"
            )
        for cleanup_error in cleanup_errors:
            exc.add_note(
                "Quarantine rollback cleanup failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )
        raise

    try:
        if _path_device_inode(claim_path) != source_owner:
            raise OSError(f"quarantine claim ownership changed: {claim_path}")
        _remove_quarantine_path(claim_path)
        if claim_path.exists() or claim_path.is_symlink():
            raise OSError(f"quarantine claim cleanup did not remove: {claim_path}")
    except BaseException as exc:
        if not _restore_owned_quarantine_claim(
            claim_path,
            original_path=storage_dir,
            owner=source_owner,
        ):
            exc.add_note(
                "Owned quarantine claim could not be restored; "
                f"inspect claim={claim_path} and original={storage_dir}"
            )
        exc.add_note(f"Complete quarantine copy is preserved at: {target}")
        raise
    return {
        "ok": True,
        "dryRun": False,
        "moved": str(storage_dir),
        "target": str(target),
        "canonicalReplaced": storage_dir.exists() or storage_dir.is_symlink(),
    }


def _path_device_inode(path: Path) -> tuple[int, int] | None:
    try:
        if path.is_symlink():
            return None
        observed = path.stat()
    except (OSError, RuntimeError):
        return None
    return int(observed.st_dev), int(observed.st_ino)


def _remove_quarantine_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _restore_owned_quarantine_claim(
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


def _restore_unowned_quarantine_claim(
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


def trash_stale_arxiv_html(
    record: dict[str, Any],
    *,
    relay: dict[str, str],
    dry_run: bool,
    delete_webdav: bool,
    timeout: int,
    deduplication_prefix: str,
) -> dict[str, Any]:
    key = str(record.get("key") or "").strip()
    library_id = str(record.get("library_id") or "").strip()
    if not key or not library_id:
        raise RuntimeError("record is missing key/library_id")
    if dry_run:
        return {
            "ok": True,
            "dryRun": True,
            "wouldTrash": True,
            "deleteWebdav": delete_webdav,
        }
    payload = {
        "libraryId": library_id,
        "dryRun": False,
        "deleteWebdav": delete_webdav,
        "deduplicationKey": f"{deduplication_prefix}:{library_id}:{key}:webdav={int(delete_webdav)}",
    }
    url = f"{relay['url'].rstrip('/')}/attachments/{urllib.parse.quote(key, safe='')}/trash"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {relay['token']}",
        },
        method="POST",
    )
    result = _relay_json_request(request, timeout=timeout)
    if not isinstance(result, dict):
        raise RuntimeError(f"relay trash response must be a JSON object: {result!r}")
    return result


def _relay_json_request(
    request: urllib.request.Request,
    *,
    timeout: int,
) -> object:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = _read_bounded_relay_bytes(
                response,
                limit=MAX_RELAY_RESPONSE_BYTES,
                label="relay",
            )
    except urllib.error.HTTPError as exc:
        try:
            error_raw = _read_bounded_relay_bytes(
                exc,
                limit=MAX_RELAY_ERROR_BYTES,
                label="relay error",
            )
            detail = error_raw.decode("utf-8", errors="replace")
        except RuntimeError as body_exc:
            detail = str(body_exc)
        raise RuntimeError(f"relay HTTP {exc.code}: {detail}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid relay JSON response: {exc}") from exc


def _read_bounded_relay_bytes(
    stream: Any,
    *,
    limit: int,
    label: str,
) -> bytes:
    raw = stream.read(limit + 1)
    if not isinstance(raw, bytes):
        raise RuntimeError(f"{label} response must be bytes")
    if len(raw) > limit:
        raise RuntimeError(f"{label} response exceeds {limit} bytes")
    return raw


def relay_library_id_for_record(record: dict[str, Any], bindings: list[Any]) -> str:
    binding = relay_binding_for_record(record, bindings)
    if binding is not None:
        return str(binding.library_id)
    return str(record.get("library_id") or "")


def relay_binding_for_record(record: dict[str, Any], bindings: list[Any]) -> Any | None:
    path = Path(str(record.get("path") or "")).resolve(strict=False)
    for binding in bindings:
        root = Path(binding.host_data_dir).resolve(strict=False)
        try:
            path.relative_to(root)
        except ValueError:
            continue
        return binding
    return None


def mark_local_attachment_deleted(
    record: dict[str, Any],
    *,
    binding: Any,
    relay_result: dict[str, Any],
) -> dict[str, Any]:
    key = str(record.get("key") or "").strip()
    if not key:
        raise RuntimeError("record is missing key")
    new_version = _exact_nonnegative_int(relay_result.get("newVersion"))
    if (
        relay_result.get("ok") is not True
        or relay_result.get("operation") != "trash_attachment"
        or relay_result.get("attachmentKey") != key
        or relay_result.get("dryRun") is not False
        or new_version is None
    ):
        raise RuntimeError(
            f"invalid relay trash result contract for {key}: {relay_result!r}"
        )
    sqlite_path = Path(binding.host_data_dir) / "zotero.sqlite"
    if sqlite_path.is_symlink() or not sqlite_path.is_file():
        raise RuntimeError(
            f"zotero.sqlite is missing or not a regular file: {sqlite_path}"
        )
    connection = sqlite3.connect(str(sqlite_path), timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "select itemID, version from items where key = ? limit 1",
            (key,),
        ).fetchone()
        if row is None:
            return {
                "ok": True,
                "updated": False,
                "reason": "local_item_missing",
                "key": key,
            }
        item_id = int(row["itemID"])
        connection.execute(
            "insert or ignore into deletedItems (itemID, dateDeleted) values (?, CURRENT_TIMESTAMP)",
            (item_id,),
        )
        connection.execute(
            "update items set version = ?, synced = 1 where itemID = ?",
            (new_version, item_id),
        )
        connection.commit()
    finally:
        connection.close()
    return {
        "ok": True,
        "updated": True,
        "sqlite_path": str(sqlite_path),
        "key": key,
        "item_id": item_id,
        "zotero_version": new_version,
    }


def run_targeted_repolish(
    *,
    keys: list[str],
    output_root: Path,
    dry_run: bool,
    request_timeout: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    unique_keys = sorted({key for key in keys if key})
    if not unique_keys:
        return {"ok": True, "dryRun": dry_run, "selected_count": 0}
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "bulk_repolish_source_html.py"),
        "--output-root",
        str(output_root),
        "--request-timeout",
        str(request_timeout),
        "--skip-unchanged",
    ]
    if dry_run:
        command.append("--dry-run")
    for key in unique_keys:
        command.extend(["--only-key", key])
    env = _source_recovery_env(os.environ.copy())
    safe_timeout = max(1, int(timeout_seconds))
    output_root.mkdir(parents=True, exist_ok=True)
    stdout_path = output_root / "subprocess.stdout.log"
    stderr_path = output_root / "subprocess.stderr.log"
    process: subprocess.Popen[bytes] | None = None
    returncode: int | None = None
    status = "start_failed"
    start_error: str | None = None
    with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
        try:
            process = subprocess.Popen(
                command,
                cwd=REPO_ROOT,
                env=env,
                stdout=stdout_file,
                stderr=stderr_file,
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                ),
                start_new_session=os.name != "nt",
            )
        except OSError as exc:
            start_error = f"{type(exc).__name__}: {exc}"
        else:
            try:
                returncode = process.wait(timeout=safe_timeout)
                status = "completed" if returncode == 0 else "failed"
            except subprocess.TimeoutExpired:
                status = "timeout"
                _terminate_process_tree(process)
            except BaseException:
                _terminate_process_tree(process)
                raise

    stdout_tail = _read_text_tail(stdout_path, max_bytes=SUBPROCESS_TAIL_BYTES)
    stderr_tail = _read_text_tail(stderr_path, max_bytes=SUBPROCESS_TAIL_BYTES)
    return {
        "ok": status == "completed" and returncode == 0,
        "status": status,
        "dryRun": dry_run,
        "keys": unique_keys,
        "output_root": str(output_root),
        "returncode": returncode,
        "timeout_seconds": safe_timeout,
        "tex_docker_image": env.get("ARXIV_SOURCE_RECOVERY_TEX_DOCKER_IMAGE", ""),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        **({"error": start_error} if start_error is not None else {}),
    }


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    try:
        if process.poll() is not None:
            return
    except OSError:
        return

    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            try:
                process.kill()
            except OSError:
                pass
    else:
        _signal_posix_process_group(process, force=False)

    try:
        process.wait(timeout=10)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    if os.name != "nt":
        _signal_posix_process_group(process, force=True)
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _signal_posix_process_group(
    process: subprocess.Popen[bytes],
    *,
    force: bool,
) -> None:
    killpg = cast(
        Callable[[int, int], None] | None,
        getattr(os, "killpg", None),
    )
    if killpg is not None:
        signal_number = int(
            getattr(signal, "SIGKILL" if force else "SIGTERM", 9 if force else 15)
        )
        try:
            killpg(process.pid, signal_number)
            return
        except OSError:
            pass
    try:
        process.kill() if force else process.terminate()
    except OSError:
        pass


def _read_text_tail(path: Path, *, max_bytes: int) -> str:
    limit = max(0, int(max_bytes))
    if limit == 0 or not path.is_file():
        return ""
    with path.open("rb") as file:
        size = file.seek(0, os.SEEK_END)
        file.seek(max(0, size - limit))
        raw = file.read(limit)
    return raw.decode("utf-8", errors="replace")


def _positive_int_arg(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _load_or_run_audit(audit_json: Path | None, *, run_root: Path) -> dict[str, Any]:
    if audit_json is not None:
        report = json.loads(
            read_text_bounded(
                audit_json,
                max_bytes=MAX_AUDIT_JSON_BYTES,
            )
        )
        if not isinstance(report, dict):
            raise RuntimeError(f"audit JSON must contain an object: {audit_json}")
        return report
    relay = bulk._relay_env()
    bindings = bulk._relay_bindings(relay)
    report = run_audit(
        zotero_data_dirs=tuple(binding.host_data_dir for binding in bindings),
        state_db=None,
        skip_job_check=True,
    )
    if not isinstance(report, dict):
        raise RuntimeError(
            f"audit runner must return an object, got {type(report).__name__}"
        )
    _write_json(run_root / "audit_input.json", report)
    return report


def _source_recovery_env(env: dict[str, str]) -> dict[str, str]:
    if env.get("ARXIV_SOURCE_RECOVERY_TEX_COMMAND") or env.get(
        "ARXIV_SOURCE_RECOVERY_TEX_DOCKER_IMAGE"
    ):
        return env
    if shutil.which("pdflatex"):
        return env
    image = _first_available_docker_tex_image()
    if image:
        env["ARXIV_SOURCE_RECOVERY_TEX_DOCKER_IMAGE"] = image
        env.setdefault("ARXIV_SOURCE_RECOVERY_TIMEOUT_SECONDS", "240")
    return env


def _first_available_docker_tex_image() -> str | None:
    if not shutil.which("docker"):
        return None
    for image in TEX_DOCKER_IMAGES:
        completed = subprocess.run(
            ["docker", "image", "inspect", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode == 0:
            return image
    return None


def _storage_dir_for_record(record: dict[str, Any]) -> Path:
    key = str(record.get("key") or "").strip()
    path = Path(str(record.get("path") or "")).resolve(strict=False)
    storage_dir = path.parent
    if (
        not key
        or storage_dir.name != key
        or storage_dir.parent.name.casefold() != "storage"
    ):
        raise RuntimeError(f"refusing to quarantine unexpected storage path: {path}")
    return storage_dir


def _exact_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _unique_records(records: Any) -> list[dict[str, Any]]:
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    iterable = records if isinstance(records, list) else []
    for record in iterable:
        if not isinstance(record, dict):
            continue
        key = (
            str(record.get("library_id") or ""),
            str(record.get("key") or ""),
            str(record.get("path") or ""),
        )
        unique[key] = record
    return list(unique.values())


def _has_issue(record: dict[str, Any], issue: str) -> bool:
    return issue in (record.get("issues") or [])


def _compact_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "library_id": record.get("library_id"),
        "key": record.get("key"),
        "parent_key": record.get("parent_key"),
        "title": record.get("title"),
        "path": record.get("path"),
        "remote_only": record.get("remote_only"),
        "remote_version": record.get("remote_version"),
        "issues": record.get("issues") or [],
    }


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 10_000):
        candidate = path.with_name(f"{path.name}.{index}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not allocate unique path for {path}")


def _safe_name(value: str) -> str:
    return (
        "".join(
            char if char.isalnum() or char in {"-", "_", "."} else "_" for char in value
        )[:160]
        or "item"
    )


def _run_root(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path(r"C:\tmp") / f"zotero_source_html_audit_cleanup_{stamp}"


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(
        temp_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    owner: tuple[int, int] | None = None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        owner = _path_device_inode(temp_path)
        if owner is None:
            raise OSError(f"atomic JSON temp file is not regular: {temp_path}")
        os.replace(temp_path, path)
    except BaseException:
        current_owner = owner or _path_device_inode(temp_path)
        if current_owner is not None and _path_device_inode(temp_path) == current_owner:
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        )
        handle.flush()
        os.fsync(handle.fileno())


if __name__ == "__main__":
    raise SystemExit(main())
