from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
from typing import Any
import shutil
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

try:  # pragma: no cover - import shape differs for script vs package execution.
    from . import bulk_repolish_source_html as bulk
except ImportError:  # pragma: no cover
    import bulk_repolish_source_html as bulk

from zotero_ingest_worker.source_html_quality_audit import run_audit  # noqa: E402


TEX_DOCKER_IMAGES = (
    "ghcr.io/xu-cheng/texlive-full:latest",
    "danteev/texlive:latest",
)


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
    parser.add_argument("--request-timeout", type=int, default=300)
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
            manifest["remote_stale_arxiv_check"] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
    _write_json(run_root / "cleanup_plan.json", plan)
    manifest["plan_counts"] = {name: len(items) for name, items in plan.items()}
    _write_json(manifest_path, manifest)

    if dry_run:
        manifest.update({"ok": True, "finished_at": _utc_now(), "results_path": str(results_path)})
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
        )
        _append_jsonl(results_path, {"action": "targeted_repolish", **repolish})
        ok = ok and bool(repolish.get("ok"))

    if plan["stale_arxiv_html"] and relay is None:
        relay = bulk._relay_env()
        relay_bindings = bulk._relay_bindings(relay)
    for record in plan["stale_arxiv_html"]:
        item: dict[str, Any] = {"action": "trash_stale_arxiv_html", "record": _compact_record(record)}
        try:
            relay_record = dict(record)
            relay_record["library_id"] = relay_library_id_for_record(record, relay_bindings)
            relay_result = trash_stale_arxiv_html(
                relay_record,
                relay=relay or {},
                dry_run=False,
                delete_webdav=args.delete_webdav,
                timeout=args.request_timeout,
                deduplication_prefix=f"source-html-audit-cleanup:{run_root.name}",
            )
            item["relay"] = relay_result
            if relay_result.get("ok"):
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
            item["ok"] = bool((item.get("relay") or {}).get("ok"))
            ok = ok and item["ok"]
        _append_jsonl(results_path, item)

    for record in plan["orphan_source_html"]:
        item = {"action": "quarantine_orphan_source_html", "record": _compact_record(record)}
        try:
            item["local_quarantine"] = quarantine_storage_dir(
                record,
                run_root=run_root,
                dry_run=False,
                label="orphan_source_html",
            )
            item["ok"] = True
        except Exception as exc:
            item["ok"] = False
            item["error"] = f"{type(exc).__name__}: {exc}"
            ok = False
        _append_jsonl(results_path, item)

    for record in plan["orphan_arxiv_html"]:
        item = {"action": "quarantine_orphan_arxiv_html", "record": _compact_record(record)}
        try:
            item["local_quarantine"] = quarantine_storage_dir(
                record,
                run_root=run_root,
                dry_run=False,
                label="orphan_arxiv_html",
            )
            item["ok"] = True
        except Exception as exc:
            item["ok"] = False
            item["error"] = f"{type(exc).__name__}: {exc}"
            ok = False
        _append_jsonl(results_path, item)

    manifest.update({"ok": ok, "finished_at": _utc_now(), "results_path": str(results_path)})
    _write_json(manifest_path, manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def cleanup_plan_from_audit(report: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    records = _unique_records(report.get("all_records") or report.get("critical_records") or [])
    return {
        "orphan_source_html": [
            record
            for record in records
            if record.get("is_source_html") and _has_issue(record, "missing_zotero_attachment_record")
        ],
        "orphan_arxiv_html": [
            record
            for record in records
            if record.get("is_arxiv_html") and _has_issue(record, "missing_zotero_attachment_record")
        ],
        "stale_arxiv_html": [
            record
            for record in records
            if record.get("is_arxiv_html") and _has_issue(record, "stale_arxiv_html_attachment")
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
        str(record.get("key") or "")
        for record in _unique_records(report.get("all_records") or report.get("critical_records") or [])
        if _has_issue(record, "stale_arxiv_html_attachment")
    }
    records: list[dict[str, Any]] = []
    for library_id, attachments in remote_by_library.items():
        source_parents = source_parent_keys.get(library_id) or set()
        if not source_parents:
            continue
        for attachment in attachments:
            key = str(attachment.get("key") or "").strip()
            parent_key = str(attachment.get("parentItem") or "").strip()
            if not key or key in existing_stale_keys or parent_key not in source_parents:
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
                    "title": str(attachment.get("title") or attachment.get("filename") or key),
                    "path": "",
                    "is_source_html": False,
                    "is_arxiv_html": True,
                    "remote_only": True,
                    "remote_version": attachment.get("version"),
                    "issues": ["stale_arxiv_html_attachment", "remote_only_arxiv_html_attachment"],
                }
            )
    return records


def source_parent_keys_by_relay_library(
    report: dict[str, Any],
    bindings: list[Any],
) -> dict[str, set[str]]:
    by_library: dict[str, set[str]] = {}
    records = _unique_records(report.get("all_records") or report.get("critical_records") or [])
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
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"relay HTTP {exc.code}: {raw}") from exc
    if not payload.get("ok"):
        raise RuntimeError(f"relay remote attachment list failed: {payload}")
    attachments = payload.get("attachments") or []
    return [item for item in attachments if isinstance(item, dict)]


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
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"relay HTTP {exc.code}: {raw}") from exc
    if not payload.get("ok"):
        raise RuntimeError(f"relay remote children list failed: {payload}")
    children = payload.get("children") or []
    return [item for item in children if isinstance(item, dict)]


def _remote_attachment_looks_like_arxiv_html(attachment: dict[str, Any]) -> bool:
    haystack = " ".join(
        str(attachment.get(field) or "") for field in ("title", "filename", "contentType")
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
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(storage_dir, target)
    shutil.rmtree(storage_dir)
    return {
        "ok": True,
        "dryRun": False,
        "moved": str(storage_dir),
        "target": str(target),
    }


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
        return {"ok": True, "dryRun": True, "wouldTrash": True, "deleteWebdav": delete_webdav}
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
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"relay HTTP {exc.code}: {raw}") from exc


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
    new_version = _optional_int(relay_result.get("newVersion"))
    sqlite_path = Path(binding.host_data_dir) / "zotero.sqlite"
    connection = sqlite3.connect(str(sqlite_path), timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "select itemID, version from items where key = ? limit 1",
            (key,),
        ).fetchone()
        if row is None:
            return {"ok": True, "updated": False, "reason": "local_item_missing", "key": key}
        item_id = int(row["itemID"])
        connection.execute(
            "insert or ignore into deletedItems (itemID, dateDeleted) values (?, CURRENT_TIMESTAMP)",
            (item_id,),
        )
        if new_version is not None:
            connection.execute(
                "update items set version = ?, synced = 1 where itemID = ?",
                (new_version, item_id),
            )
        else:
            connection.execute("update items set synced = 1 where itemID = ?", (item_id,))
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
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    return {
        "ok": completed.returncode == 0,
        "dryRun": dry_run,
        "keys": unique_keys,
        "output_root": str(output_root),
        "returncode": completed.returncode,
        "tex_docker_image": env.get("ARXIV_SOURCE_RECOVERY_TEX_DOCKER_IMAGE", ""),
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
    }


def _load_or_run_audit(audit_json: Path | None, *, run_root: Path) -> dict[str, Any]:
    if audit_json is not None:
        return json.loads(audit_json.read_text(encoding="utf-8"))
    relay = bulk._relay_env()
    bindings = bulk._relay_bindings(relay)
    report = run_audit(
        zotero_data_dirs=tuple(binding.host_data_dir for binding in bindings),
        state_db=None,
        skip_job_check=True,
    )
    _write_json(run_root / "audit_input.json", report)
    return report


def _source_recovery_env(env: dict[str, str]) -> dict[str, str]:
    if env.get("ARXIV_SOURCE_RECOVERY_TEX_COMMAND") or env.get("ARXIV_SOURCE_RECOVERY_TEX_DOCKER_IMAGE"):
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
    if not key or storage_dir.name != key or storage_dir.parent.name.casefold() != "storage":
        raise RuntimeError(f"refusing to quarantine unexpected storage path: {path}")
    return storage_dir


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
    return "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in value)[:160] or "item"


def _run_root(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path(r"C:\tmp") / f"zotero_source_html_audit_cleanup_{stamp}"


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
