from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from zoteropdf2md.web_html_polish import polish_web_html_file  # noqa: E402


ARXIV_ID_RE = re.compile(
    r"(?i)(?:arxiv:|10\.48550/arxiv\.)([a-z.-]+/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?"
)
BARE_ARXIV_ID_RE = re.compile(r"(?i)^(?:[a-z.-]+/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?$")
MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
MAX_RELAY_RESPONSE_BYTES = 1024 * 1024
MAX_RELAY_ERROR_BYTES = 64 * 1024
MAX_RESULTS_JSONL_LINE_BYTES = 1024 * 1024
MAX_COMPLETED_KEYS = 1_000_000
SQLITE_INTEGER_MAX = (1 << 63) - 1


SOURCE_HTML_SQL = """
select
  i.itemID,
  i.key,
  i.version,
  i.libraryID as localLibraryID,
  ia.parentItemID,
  parent.key as parentKey,
  ia.contentType,
  ia.path,
  coalesce(v.value, '') as title,
  coalesce((select pv.value from itemData pd join fields pf on pf.fieldID = pd.fieldID join itemDataValues pv on pv.valueID = pd.valueID where pd.itemID = ia.parentItemID and pf.fieldName = 'url' limit 1), '') as parentUrl,
  coalesce((select pv.value from itemData pd join fields pf on pf.fieldID = pd.fieldID join itemDataValues pv on pv.valueID = pd.valueID where pd.itemID = ia.parentItemID and pf.fieldName = 'DOI' limit 1), '') as parentDoi,
  coalesce((select pv.value from itemData pd join fields pf on pf.fieldID = pd.fieldID join itemDataValues pv on pv.valueID = pd.valueID where pd.itemID = ia.parentItemID and pf.fieldName = 'archiveID' limit 1), '') as parentArchiveId,
  coalesce((select pv.value from itemData pd join fields pf on pf.fieldID = pd.fieldID join itemDataValues pv on pv.valueID = pd.valueID where pd.itemID = ia.parentItemID and pf.fieldName = 'archiveLocation' limit 1), '') as parentArchiveLocation,
  coalesce((select pv.value from itemData pd join fields pf on pf.fieldID = pd.fieldID join itemDataValues pv on pv.valueID = pd.valueID where pd.itemID = ia.parentItemID and pf.fieldName = 'extra' limit 1), '') as parentExtra
from items i
join itemAttachments ia on ia.itemID = i.itemID
left join items parent on parent.itemID = ia.parentItemID
left join deletedItems di on di.itemID = i.itemID
left join itemData d on d.itemID = i.itemID
  and d.fieldID = (select fieldID from fields where fieldName = 'title' limit 1)
left join itemDataValues v on v.valueID = d.valueID
where di.itemID is null
  and lower(coalesce(ia.contentType, '')) in ('text/html', 'application/xhtml+xml')
  and (
    upper(coalesce(v.value, '')) like '%SOURCE HTML%'
    or upper(coalesce(ia.path, '')) like '%SOURCE HTML%'
  )
order by i.key
"""

HTML_ATTACHMENT_SQL = """
select
  i.itemID,
  i.key,
  i.version,
  i.libraryID as localLibraryID,
  ia.parentItemID,
  parent.key as parentKey,
  ia.contentType,
  ia.path,
  coalesce(v.value, '') as title,
  coalesce((select pv.value from itemData pd join fields pf on pf.fieldID = pd.fieldID join itemDataValues pv on pv.valueID = pd.valueID where pd.itemID = ia.parentItemID and pf.fieldName = 'url' limit 1), '') as parentUrl,
  coalesce((select pv.value from itemData pd join fields pf on pf.fieldID = pd.fieldID join itemDataValues pv on pv.valueID = pd.valueID where pd.itemID = ia.parentItemID and pf.fieldName = 'DOI' limit 1), '') as parentDoi,
  coalesce((select pv.value from itemData pd join fields pf on pf.fieldID = pd.fieldID join itemDataValues pv on pv.valueID = pd.valueID where pd.itemID = ia.parentItemID and pf.fieldName = 'archiveID' limit 1), '') as parentArchiveId,
  coalesce((select pv.value from itemData pd join fields pf on pf.fieldID = pd.fieldID join itemDataValues pv on pv.valueID = pd.valueID where pd.itemID = ia.parentItemID and pf.fieldName = 'archiveLocation' limit 1), '') as parentArchiveLocation,
  coalesce((select pv.value from itemData pd join fields pf on pf.fieldID = pd.fieldID join itemDataValues pv on pv.valueID = pd.valueID where pd.itemID = ia.parentItemID and pf.fieldName = 'extra' limit 1), '') as parentExtra
from items i
join itemAttachments ia on ia.itemID = i.itemID
left join items parent on parent.itemID = ia.parentItemID
left join deletedItems di on di.itemID = i.itemID
left join itemData d on d.itemID = i.itemID
  and d.fieldID = (select fieldID from fields where fieldName = 'title' limit 1)
left join itemDataValues v on v.valueID = d.valueID
where di.itemID is null
  and lower(coalesce(ia.contentType, '')) in ('text/html', 'application/xhtml+xml')
order by i.key
"""


@dataclass(frozen=True)
class RelayBinding:
    library_id: str
    name: str
    host_data_dir: Path
    container_data_dir: str


@dataclass(frozen=True)
class SourceAttachment:
    binding: RelayBinding
    item_id: int
    key: str
    version: int
    local_library_id: int
    parent_key: str | None
    title: str
    zotero_path: str
    host_path: Path
    container_path: str
    source_url: str | None

    @property
    def filename(self) -> str:
        return self.host_path.name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Repolish active Zotero [SOURCE HTML] attachments and upload them to WebDAV via zotero-file-relay."
    )
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--only-key", action="append", default=[])
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-unchanged", action="store_true")
    parser.add_argument(
        "--include-non-source-html",
        action="store_true",
        help="Allow --only-key to select active HTML attachments whose title/path is not [SOURCE HTML].",
    )
    parser.add_argument("--request-timeout", type=int, default=300)
    args = parser.parse_args(argv)

    relay = _relay_env()
    bindings = _relay_bindings(relay)
    run_root = _run_root(args.output_root)
    run_root.mkdir(parents=True, exist_ok=True)
    manifest_path = run_root / "manifest.json"
    results_path = run_root / "results.jsonl"
    completed = _completed_keys(results_path) if args.resume else set()
    only_keys = {str(key).strip() for key in args.only_key if str(key).strip()}

    attachments = _discover_attachments(
        bindings, source_only=not args.include_non_source_html
    )
    if only_keys:
        attachments = [
            attachment for attachment in attachments if attachment.key in only_keys
        ]
    if args.resume:
        attachments = [
            attachment for attachment in attachments if attachment.key not in completed
        ]
    if args.limit is not None:
        attachments = attachments[: max(0, args.limit)]

    manifest: dict[str, Any] = {
        "ok": None,
        "run_root": str(run_root),
        "dry_run": bool(args.dry_run),
        "skip_unchanged": bool(args.skip_unchanged),
        "include_non_source_html": bool(args.include_non_source_html),
        "started_at": _utc_now(),
        "relay_url": relay["url"],
        "bindings": [
            {
                "library_id": binding.library_id,
                "name": binding.name,
                "host_data_dir": str(binding.host_data_dir),
                "container_data_dir": binding.container_data_dir,
            }
            for binding in bindings
        ],
        "selected_count": len(attachments),
        "sqlite_backups": [],
    }
    _write_json(manifest_path, manifest)

    if args.dry_run:
        planned = [_attachment_plan(attachment) for attachment in attachments]
        _write_json(run_root / "dry_run_plan.json", {"attachments": planned})
        manifest.update(
            {
                "ok": True,
                "finished_at": _utc_now(),
                "planned_count": len(planned),
                "planned_bytes": sum(item["bytes"] for item in planned),
            }
        )
        _write_json(manifest_path, manifest)
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0

    backed_up_sqlite: set[Path] = set()
    counts = {"processed": 0, "uploaded": 0, "unchanged": 0, "failed": 0}
    for index, attachment in enumerate(attachments, start=1):
        started = time.time()
        record: dict[str, Any] = {
            "ok": False,
            "index": index,
            "count": len(attachments),
            "library_id": attachment.binding.library_id,
            "library_name": attachment.binding.name,
            "key": attachment.key,
            "parent_key": attachment.parent_key,
            "title": attachment.title,
            "path": str(attachment.host_path),
            "started_at": _utc_now(),
        }
        tmp_path: Path | None = None
        try:
            sqlite_path = attachment.binding.host_data_dir / "zotero.sqlite"
            if sqlite_path not in backed_up_sqlite:
                backup = _sqlite_backup(
                    sqlite_path,
                    run_root
                    / "sqlite"
                    / f"{_safe_name(attachment.binding.library_id)}.sqlite",
                )
                manifest["sqlite_backups"].append(backup)
                _write_json(manifest_path, manifest)
                backed_up_sqlite.add(sqlite_path)

            before_hash = _sha256(attachment.host_path)
            before_stat = attachment.host_path.stat()
            tmp_path = attachment.host_path.with_name(
                f".__z2m_bulk_repolish_{os.getpid()}_{attachment.key}.html"
            )
            polished = polish_web_html_file(
                attachment.host_path, source_url=attachment.source_url
            )
            tmp_path.write_text(polished.html, encoding="utf-8")
            after_hash = _sha256(tmp_path)
            after_stat = tmp_path.stat()
            record.update(
                {
                    "polish": {
                        "kind": str(polished.kind),
                        "article_extracted": polished.article_extracted,
                        "article_selector": polished.article_selector,
                        "inlined_images": polished.inlined_images,
                        "recovered_source_figures": polished.recovered_source_figures,
                        "attempted_source_figures": polished.attempted_source_figures,
                        "source_recovery_errors": list(polished.source_recovery_errors),
                        "source_url": attachment.source_url,
                    },
                    "before": {
                        "bytes": before_stat.st_size,
                        "mtime_ns": before_stat.st_mtime_ns,
                        "sha256": before_hash,
                    },
                    "after": {
                        "bytes": after_stat.st_size,
                        "mtime_ns": after_stat.st_mtime_ns,
                        "sha256": after_hash,
                    },
                }
            )
            if args.skip_unchanged and before_hash == after_hash:
                record.update(
                    {"ok": True, "status": "unchanged", "finished_at": _utc_now()}
                )
                counts["unchanged"] += 1
                _append_jsonl(results_path, record)
                print(_progress_line(record, started))
                tmp_path.unlink(missing_ok=True)
                continue

            backup_path = _backup_attachment_file(run_root, attachment)
            container_tmp = _host_to_container_path(tmp_path, attachment.binding)
            relay_result = _relay_replace(
                relay=relay,
                attachment=attachment,
                source_path=container_tmp,
                expected_old_sha256=before_hash,
                deduplication_key=(
                    f"bulk-repolish-source-html:{run_root.name}:"
                    f"{attachment.binding.library_id}:{attachment.key}:{after_hash[:16]}"
                ),
                timeout=args.request_timeout,
            )
            os.replace(tmp_path, attachment.host_path)
            local_metadata = _sync_local_storage_metadata(
                attachment=attachment,
                relay_result=relay_result,
            )
            if (
                local_metadata.get("ok") is not True
                or local_metadata.get("updated") is not True
            ):
                raise RuntimeError(f"local metadata sync failed: {local_metadata!r}")
            record.update(
                {
                    "ok": True,
                    "status": "uploaded",
                    "backup_path": str(backup_path),
                    "relay": _redacted_relay_result(relay_result),
                    "local_metadata": local_metadata,
                    "finished_at": _utc_now(),
                }
            )
            counts["uploaded"] += 1
        except Exception as exc:
            counts["failed"] += 1
            record.update(
                {
                    "ok": False,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "finished_at": _utc_now(),
                }
            )
            try:
                if tmp_path is not None and tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
        finally:
            counts["processed"] += 1
            _append_jsonl(results_path, record)
            print(_progress_line(record, started), flush=True)

    manifest.update(
        {
            "ok": counts["failed"] == 0,
            "finished_at": _utc_now(),
            "counts": counts,
            "results_path": str(results_path),
        }
    )
    _write_json(manifest_path, manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if counts["failed"] == 0 else 1


def _relay_env() -> dict[str, str]:
    raw = subprocess.check_output(
        ["docker", "inspect", "zotero-file-relay", "--format", "{{json .Config.Env}}"]
    ).decode("utf-8", errors="replace")
    values: dict[str, str] = {}
    for item in json.loads(raw):
        if "=" in item:
            key, value = item.split("=", 1)
            values[key] = value
    token = values.get("ZFR_TOKEN", "")
    if not token:
        raise RuntimeError("zotero-file-relay has no ZFR_TOKEN.")
    return {
        "url": f"http://127.0.0.1:{values.get('ZFR_HTTP_PORT', '23119')}",
        "token": token,
        "bindings": values.get("ZFR_LIBRARY_BINDINGS", "[]"),
    }


def _relay_bindings(relay: dict[str, str]) -> list[RelayBinding]:
    bindings: list[RelayBinding] = []
    for item in json.loads(relay["bindings"]):
        library_id = str(item.get("libraryId") or "").strip()
        host_data_dir = Path(str(item.get("hostDataDir") or ""))
        container_data_dir = str(item.get("dataDir") or "").strip()
        if not library_id or not container_data_dir:
            continue
        bindings.append(
            RelayBinding(
                library_id=library_id,
                name=str(item.get("name") or library_id),
                host_data_dir=host_data_dir,
                container_data_dir=container_data_dir.rstrip("/"),
            )
        )
    return bindings


def _discover_attachments(
    bindings: list[RelayBinding], *, source_only: bool = True
) -> list[SourceAttachment]:
    attachments: list[SourceAttachment] = []
    query = SOURCE_HTML_SQL if source_only else HTML_ATTACHMENT_SQL
    for binding in bindings:
        db_path = binding.host_data_dir / "zotero.sqlite"
        if not db_path.is_file():
            continue
        storage_dir = binding.host_data_dir / "storage"
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(query).fetchall()
        finally:
            connection.close()
        for row in rows:
            host_path = _resolve_host_attachment_path(
                storage_dir=storage_dir,
                key=str(row["key"]),
                zotero_path=str(row["path"] or ""),
            )
            if not host_path.is_file():
                continue
            attachments.append(
                SourceAttachment(
                    binding=binding,
                    item_id=int(row["itemID"]),
                    key=str(row["key"]),
                    version=int(row["version"] or 0),
                    local_library_id=int(row["localLibraryID"] or 0),
                    parent_key=str(row["parentKey"]) if row["parentKey"] else None,
                    title=str(row["title"] or host_path.stem),
                    zotero_path=str(row["path"] or ""),
                    host_path=host_path,
                    container_path=_host_to_container_path(host_path, binding),
                    source_url=_source_url_hint(
                        parent_url=str(row["parentUrl"] or ""),
                        parent_doi=str(row["parentDoi"] or ""),
                        parent_archive_id=str(row["parentArchiveId"] or ""),
                        parent_archive_location=str(row["parentArchiveLocation"] or ""),
                        parent_extra=str(row["parentExtra"] or ""),
                    ),
                )
            )
    return attachments


def _resolve_host_attachment_path(
    *, storage_dir: Path, key: str, zotero_path: str
) -> Path:
    if zotero_path.startswith("storage:"):
        return storage_dir / key / zotero_path.removeprefix("storage:")
    if zotero_path:
        return Path(zotero_path)
    htmls = sorted(
        (storage_dir / key).glob("*.html"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if htmls:
        return htmls[0]
    return storage_dir / key / f"{key}.html"


def _source_url_hint(
    *,
    parent_url: str,
    parent_doi: str,
    parent_archive_id: str,
    parent_archive_location: str,
    parent_extra: str,
) -> str | None:
    for value in (
        parent_archive_id,
        parent_archive_location,
        parent_doi,
        parent_extra,
        parent_url,
    ):
        arxiv_id = _arxiv_id_from_metadata_value(value)
        if arxiv_id:
            return f"https://arxiv.org/html/{arxiv_id}"
    url = parent_url.strip()
    if url.startswith(("http://", "https://")):
        return url
    doi = parent_doi.strip()
    if doi.startswith("10."):
        return f"https://doi.org/{doi}"
    return None


def _arxiv_id_from_metadata_value(value: str) -> str | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    match = ARXIV_ID_RE.search(cleaned)
    if match is not None:
        return match.group(1)
    if BARE_ARXIV_ID_RE.match(cleaned):
        return re.sub(r"v\d+$", "", cleaned, flags=re.IGNORECASE)
    return None


def _host_to_container_path(path: Path, binding: RelayBinding) -> str:
    resolved = path.resolve(strict=False)
    root = binding.host_data_dir.resolve(strict=False)
    try:
        rel = resolved.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(
            f"Path is outside relay binding {binding.host_data_dir}: {path}"
        ) from exc
    return str(PurePosixPath(binding.container_data_dir, *rel.parts))


def _relay_replace(
    *,
    relay: dict[str, str],
    attachment: SourceAttachment,
    source_path: str,
    expected_old_sha256: str,
    deduplication_key: str,
    timeout: int,
) -> dict[str, Any]:
    expected_version = _exact_nonnegative_int(attachment.version)
    if expected_version is None:
        raise RuntimeError(
            f"invalid local attachment version for relay replace: {attachment.version!r}"
        )
    if SHA256_RE.fullmatch(expected_old_sha256) is None:
        raise RuntimeError("expected old attachment SHA-256 is invalid")
    payload = {
        "sourcePath": source_path,
        "filename": attachment.filename,
        "expectedOldSha256": expected_old_sha256.lower(),
        "expectedVersion": expected_version,
        "libraryId": attachment.binding.library_id,
        "strategy": "webdav_only",
        "deduplicationKey": deduplication_key,
    }
    url = f"{relay['url'].rstrip('/')}/attachments/{urllib.parse.quote(attachment.key, safe='')}/file"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {relay['token']}",
        },
        method="PUT",
    )
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
        parsed: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid relay JSON response: {exc}") from exc
    return _validated_relay_replace_result(parsed, attachment=attachment)


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


def _string_keyed_mapping(value: object, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise RuntimeError(f"invalid relay replace result: {field} must be an object")
    return cast(dict[str, Any], value)


def _validated_relay_replace_result(
    value: object,
    *,
    attachment: SourceAttachment,
) -> dict[str, Any]:
    result = _string_keyed_mapping(value, field="response")
    if result.get("ok") is not True:
        raise RuntimeError("invalid relay replace result: ok must be true")
    operation_id = _exact_nonnegative_int(result.get("operationId"))
    if operation_id is None or operation_id == 0:
        raise RuntimeError(
            "invalid relay replace result: operationId must be a positive integer"
        )
    if result.get("attachmentKey") != attachment.key:
        raise RuntimeError("invalid relay replace result: attachmentKey mismatch")
    if result.get("strategy") != "webdav_only":
        raise RuntimeError("invalid relay replace result: strategy mismatch")
    if result.get("dryRun") is not False:
        raise RuntimeError("invalid relay replace result: dryRun must be false")

    webdav = _string_keyed_mapping(result.get("webDav"), field="webDav")
    if webdav.get("ok") is not True:
        raise RuntimeError("invalid relay replace result: webDav.ok must be true")
    if webdav.get("filename") != attachment.filename:
        raise RuntimeError("invalid relay replace result: webDav.filename mismatch")
    md5 = webdav.get("md5")
    if not isinstance(md5, str) or MD5_RE.fullmatch(md5.strip()) is None:
        raise RuntimeError("invalid relay replace result: webDav.md5 is invalid")
    if _exact_nonnegative_int(webdav.get("mtime")) is None:
        raise RuntimeError(
            "invalid relay replace result: webDav.mtime must be an exact integer"
        )

    metadata_patch = _string_keyed_mapping(
        webdav.get("metadataPatch"),
        field="webDav.metadataPatch",
    )
    if metadata_patch.get("ok") is not True:
        raise RuntimeError(
            "invalid relay replace result: webDav.metadataPatch.ok must be true"
        )
    previous_version = _exact_nonnegative_int(metadata_patch.get("previousVersion"))
    new_version = _exact_nonnegative_int(metadata_patch.get("newVersion"))
    if previous_version != attachment.version:
        raise RuntimeError(
            "invalid relay replace result: metadata previousVersion mismatch"
        )
    if new_version is None or new_version <= previous_version:
        raise RuntimeError(
            "invalid relay replace result: metadata newVersion must advance"
        )
    return result


def _sync_local_storage_metadata(
    *,
    attachment: SourceAttachment,
    relay_result: dict[str, Any],
) -> dict[str, Any]:
    validated = _validated_relay_replace_result(
        relay_result,
        attachment=attachment,
    )
    webdav = cast(dict[str, Any], validated["webDav"])
    metadata_patch = cast(dict[str, Any], webdav["metadataPatch"])
    storage_hash = cast(str, webdav["md5"]).strip().lower()
    storage_mtime = cast(int, webdav["mtime"])
    new_version = cast(int, metadata_patch["newVersion"])

    sqlite_path = attachment.binding.host_data_dir / "zotero.sqlite"
    if sqlite_path.is_symlink() or not sqlite_path.is_file():
        raise RuntimeError(f"local zotero.sqlite is not a regular file: {sqlite_path}")
    connection = sqlite3.connect(str(sqlite_path), timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("begin immediate")
        identity = connection.execute(
            """
            select i.itemID, i.libraryID, i.key, i.version
            from itemAttachments ia
            join items i on i.itemID = ia.itemID
            where ia.itemID = ?
            limit 1
            """,
            (attachment.item_id,),
        ).fetchone()
        if identity is None:
            raise RuntimeError(f"local attachment row is missing: {attachment.key}")
        if (
            identity["libraryID"] != attachment.local_library_id
            or identity["key"] != attachment.key
        ):
            raise RuntimeError(f"local attachment identity changed: {attachment.key}")
        observed_version = _optional_int(identity["version"])
        if observed_version != attachment.version:
            raise RuntimeError(
                "local attachment version changed: "
                f"expected {attachment.version}, observed {observed_version}"
            )
        before_cache = connection.execute(
            """
            select version, data
            from syncCache
            where libraryID = ? and key = ? and syncObjectTypeID = 3
            limit 1
            """,
            (attachment.local_library_id, attachment.key),
        ).fetchone()
        patched: str | None = None
        if before_cache is not None:
            patched = _patched_sync_cache_json(
                str(before_cache["data"] or ""),
                attachment_key=attachment.key,
                version=new_version,
                storage_hash=storage_hash,
                storage_mtime=storage_mtime,
            )
            if patched is None:
                raise RuntimeError(f"local syncCache JSON is invalid: {attachment.key}")

        attachment_cursor = connection.execute(
            """
            update itemAttachments
            set storageHash = ?, storageModTime = ?, syncState = 2
            where itemID = ?
            """,
            (storage_hash, storage_mtime, attachment.item_id),
        )
        if attachment_cursor.rowcount != 1:
            raise RuntimeError(
                f"local attachment update lost identity: {attachment.key}"
            )
        item_cursor = connection.execute(
            """
            update items
            set version = ?, synced = 1
            where itemID = ? and libraryID = ? and key = ? and version = ?
            """,
            (
                new_version,
                attachment.item_id,
                attachment.local_library_id,
                attachment.key,
                attachment.version,
            ),
        )
        if item_cursor.rowcount != 1:
            raise RuntimeError(f"local item update lost identity: {attachment.key}")
        cache_updated = False
        if before_cache is not None and patched is not None:
            cache_cursor = connection.execute(
                """
                    update syncCache
                    set version = ?, data = ?
                    where libraryID = ? and key = ? and syncObjectTypeID = 3
                    """,
                (new_version, patched, attachment.local_library_id, attachment.key),
            )
            if cache_cursor.rowcount != 1:
                raise RuntimeError(
                    f"local syncCache update lost identity: {attachment.key}"
                )
            cache_updated = True
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "ok": True,
        "updated": True,
        "sqlite_path": str(sqlite_path),
        "item_id": attachment.item_id,
        "zotero_version": new_version,
        "storage_hash": storage_hash,
        "storage_mtime": storage_mtime,
        "sync_cache_updated": cache_updated,
    }


def _patched_sync_cache_json(
    raw: str,
    *,
    attachment_key: str,
    version: int,
    storage_hash: str,
    storage_mtime: int,
) -> str | None:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    payload["version"] = version
    data = payload.get("data")
    if not isinstance(data, dict):
        data = {}
        payload["data"] = data
    data["key"] = data.get("key") or attachment_key
    data["version"] = version
    data["md5"] = storage_hash
    data["mtime"] = storage_mtime
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _backup_attachment_file(run_root: Path, attachment: SourceAttachment) -> Path:
    target = (
        run_root
        / "storage_backups"
        / _safe_name(attachment.binding.library_id)
        / attachment.key
        / attachment.filename
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(attachment.host_path, target)
    return target


def _sqlite_backup(src: Path, dst: Path) -> dict[str, Any]:
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    target = sqlite3.connect(str(dst), timeout=30)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return {"src": str(src), "dst": str(dst), "bytes": dst.stat().st_size}


def _attachment_plan(attachment: SourceAttachment) -> dict[str, Any]:
    stat = attachment.host_path.stat()
    return {
        "library_id": attachment.binding.library_id,
        "library_name": attachment.binding.name,
        "key": attachment.key,
        "parent_key": attachment.parent_key,
        "title": attachment.title,
        "path": str(attachment.host_path),
        "container_path": attachment.container_path,
        "source_url": attachment.source_url,
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _run_root(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path(r"C:\tmp") / f"zotero_bulk_source_repolish_{stamp}"


def _completed_keys(results_path: Path) -> set[str]:
    if not results_path.is_file():
        return set()
    completed: set[str] = set()
    with results_path.open("rb") as stream:
        while True:
            raw = stream.readline(MAX_RESULTS_JSONL_LINE_BYTES + 1)
            if not raw:
                break
            if len(raw) > MAX_RESULTS_JSONL_LINE_BYTES:
                _discard_jsonl_line_tail(stream, raw)
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict) or item.get("ok") is not True:
                continue
            key_value = item.get("key")
            if not isinstance(key_value, str) or not key_value.strip():
                continue
            key = key_value.strip()
            if key not in completed and len(completed) >= MAX_COMPLETED_KEYS:
                raise RuntimeError(
                    f"resume state exceeds {MAX_COMPLETED_KEYS} completed keys"
                )
            completed.add(key)
    return completed


def _discard_jsonl_line_tail(stream: Any, first_chunk: bytes) -> None:
    chunk = first_chunk
    while chunk and not chunk.endswith(b"\n"):
        chunk = stream.readline(MAX_RESULTS_JSONL_LINE_BYTES + 1)


def _redacted_relay_result(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if key not in {"operationId"}}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= SQLITE_INTEGER_MAX else None
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 19
        or not normalized.isascii()
        or not normalized.isdecimal()
    ):
        return None
    parsed = int(normalized)
    return parsed if parsed <= SQLITE_INTEGER_MAX else None


def _exact_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= SQLITE_INTEGER_MAX else None


def _safe_name(value: str) -> str:
    return (
        "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value) or "value"
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        fh.write("\n")


def _progress_line(record: dict[str, Any], started: float) -> str:
    return (
        f"[{record.get('index')}/{record.get('count')}] "
        f"{record.get('status')} {record.get('key')} "
        f"{time.time() - started:.1f}s"
    )


if __name__ == "__main__":
    raise SystemExit(main())
