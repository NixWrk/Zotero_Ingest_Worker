from __future__ import annotations

import argparse
import asyncio
import json
import re
import stat
import sys
import urllib.parse
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from zotero_ingest_worker.config import WorkerConfig, from_env  # noqa: E402
from zotero_ingest_worker.browser_network_policy import (  # noqa: E402
    BrowserNetworkAudit,
    ResolveTarget,
    install_browser_network_policy,
    redact_network_url,
    validate_researchgate_initial_url,
)
from zotero_ingest_worker.full_text_attachment import (  # noqa: E402
    _create_parent_attachment_source_snapshot,
    write_parent_attachment_local_copy,
)
from zotero_ingest_worker.article_standard import (  # noqa: E402
    _stable_file_fingerprint,
)
from zotero_ingest_worker.local_attachment_sync import (  # noqa: E402
    sync_parent_attachment_local,
)
from zotero_ingest_worker.local_zotero import (  # noqa: E402
    LocalAttachment,
    LocalItemMetadata,
    LocalZoteroStore,
)
from zotero_ingest_worker.filename_safety import (  # noqa: E402
    safe_filename_component,
)
from zotero_ingest_worker.safe_connect_proxy import SafeConnectProxy  # noqa: E402
from zotero_ingest_worker.relay_client import ZoteroRelayClient  # noqa: E402


DEFAULT_PROFILE_DIR = PROJECT_ROOT / "data" / "browser" / "researchgate"
DEFAULT_DOWNLOAD_DIR = (
    PROJECT_ROOT / "data" / "ingest" / "researchgate_browser_downloads"
)
DEFAULT_MAX_PDF_BYTES = 120_000_000
DOWNLOAD_TEXT_RE = re.compile(
    r"(download\s+(full[-\s]?text\s+)?pdf|download\s+pdf|full[-\s]?text\s+pdf)",
    re.IGNORECASE,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download a ResearchGate PDF through a real browser session and optionally attach it to Zotero.",
    )
    parser.add_argument("--url", required=True, help="ResearchGate publication URL.")
    parser.add_argument(
        "--item-key",
        default="",
        help="Zotero parent item key to attach the downloaded PDF to.",
    )
    parser.add_argument(
        "--data-dir",
        default="",
        help="Optional Zotero data dir when item key is not unique.",
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_DOWNLOAD_DIR))
    parser.add_argument("--profile-dir", default=str(DEFAULT_PROFILE_DIR))
    parser.add_argument(
        "--channel",
        default="msedge",
        help="Playwright browser channel: msedge, chrome, chromium, etc.",
    )
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=90)
    parser.add_argument("--max-pdf-bytes", type=int, default=DEFAULT_MAX_PDF_BYTES)
    parser.add_argument(
        "--manual-timeout-seconds",
        type=int,
        default=180,
        help="If automatic click fails, wait this long for a manual browser click/download.",
    )
    parser.add_argument(
        "--attach",
        action="store_true",
        help="Attach the downloaded PDF to the Zotero parent item.",
    )
    parser.add_argument(
        "--force-attach",
        action="store_true",
        help="Attach even if Zotero already shows a PDF.",
    )
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="Leave browser open for debugging after download.",
    )
    args = parser.parse_args()

    result = asyncio.run(run(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") is True else 1


async def run(args: argparse.Namespace) -> dict[str, Any]:
    config: WorkerConfig | None = None
    if args.attach:
        if not args.item_key.strip():
            return {"ok": False, "status": "item_key_required_for_attach"}
        config = from_env()
        preflight = preflight_pdf_attach(
            config,
            item_key=args.item_key.strip(),
            data_dir=args.data_dir.strip(),
            force=bool(args.force_attach),
        )
        if not isinstance(preflight, dict):
            return {
                "ok": False,
                "status": "preflight_invalid_result",
                "preflight": preflight,
            }
        preflight_ok = preflight.get("ok")
        skipped = preflight.get("skipped")
        if preflight_ok is False:
            return {
                "ok": False,
                "status": str(preflight.get("status") or "preflight_failed"),
                "preflight": preflight,
            }
        if preflight_ok is not True or (skipped is not True and skipped is not False):
            return {
                "ok": False,
                "status": "preflight_invalid_result",
                "preflight": preflight,
            }
        if skipped is True:
            return {
                "ok": True,
                "status": str(preflight.get("status") or "attach_skipped"),
                "download": {
                    "ok": True,
                    "skipped": True,
                    "reason": preflight.get("status"),
                },
                "attach": preflight,
            }

    try:
        download = await download_researchgate_pdf(
            url=args.url,
            output_dir=Path(args.output_dir),
            profile_dir=Path(args.profile_dir),
            item_key=args.item_key,
            channel=args.channel,
            headless=bool(args.headless),
            timeout_seconds=max(1, int(args.timeout_seconds)),
            manual_timeout_seconds=max(0, int(args.manual_timeout_seconds)),
            keep_open=bool(args.keep_open),
            max_pdf_bytes=max(
                1,
                int(getattr(args, "max_pdf_bytes", DEFAULT_MAX_PDF_BYTES)),
            ),
        )
    except ModuleNotFoundError as exc:
        if exc.name == "playwright":
            return {
                "ok": False,
                "status": "playwright_missing",
                "error": "Install Playwright first: python -m pip install playwright",
            }
        raise

    if not isinstance(download, dict):
        return {
            "ok": False,
            "status": "download_invalid_result",
            "download": download,
        }
    download_ok = download.get("ok")
    if download_ok is not True:
        return {
            "ok": False,
            "status": (
                str(
                    download.get("status")
                    or download.get("reason")
                    or "download_failed"
                )
                if download_ok is False
                else "download_invalid_result"
            ),
            "download": download,
        }
    payload: dict[str, Any] = {
        "ok": True,
        "status": download.get("status"),
        "download": download,
    }
    if not args.attach:
        return payload
    output_path_value = download.get("output_path")
    output_path = (
        output_path_value.strip() if isinstance(output_path_value, str) else ""
    )
    source_path = Path(output_path) if output_path else None
    if source_path is None or source_path.is_symlink() or not source_path.is_file():
        payload["ok"] = False
        payload["status"] = "download_invalid_result"
        return payload
    if config is None:
        config = from_env()
    attach = attach_pdf_to_zotero_parent(
        config,
        item_key=args.item_key.strip(),
        source_path=source_path,
        data_dir=args.data_dir.strip(),
        force=bool(args.force_attach),
    )
    payload["attach"] = attach
    if not isinstance(attach, dict) or attach.get("ok") is not True:
        payload["ok"] = False
        payload["status"] = (
            str(attach.get("status") or attach.get("reason") or "attach_failed")
            if isinstance(attach, dict) and attach.get("ok") is False
            else "attach_invalid_result"
        )
        return payload
    payload["ok"] = True
    payload["status"] = "attached"
    return payload


def preflight_pdf_attach(
    config: WorkerConfig,
    *,
    item_key: str,
    data_dir: str,
    force: bool,
) -> dict[str, Any]:
    metadata, store = find_item(config, item_key=item_key, data_dir=data_dir)
    inventory = store.item_full_text_inventory(metadata)
    if inventory.get("has_pdf") is True and not force:
        return {
            "ok": True,
            "skipped": True,
            "status": "parent_already_has_pdf",
            "item_key": item_key,
            "inventory": inventory,
        }
    return {
        "ok": True,
        "skipped": False,
        "status": "attach_allowed",
        "item_key": item_key,
        "inventory": inventory,
    }


async def download_researchgate_pdf(
    *,
    url: str,
    output_dir: Path,
    profile_dir: Path,
    item_key: str,
    channel: str,
    headless: bool,
    timeout_seconds: int,
    manual_timeout_seconds: int,
    keep_open: bool,
    resolve_target: ResolveTarget | None = None,
    max_pdf_bytes: int = DEFAULT_MAX_PDF_BYTES,
) -> dict[str, Any]:
    network_audit = BrowserNetworkAudit()
    initial_decision = await asyncio.to_thread(
        validate_researchgate_initial_url,
        url,
        resolve_target=resolve_target,
    )
    network_audit.record(initial_decision)
    if not initial_decision.allowed:
        return {
            "ok": False,
            "status": "unsafe_browser_url",
            "url": initial_decision.to_audit_dict()["url"],
            "reason": initial_decision.reason,
            "network_policy": network_audit.to_dict(),
        }

    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright

    output_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)
    target_prefix = safe_filename_part(item_key or "researchgate")

    async with (
        SafeConnectProxy(resolve_target=resolve_target) as connect_proxy,
        async_playwright() as p,
    ):
        launch_options: dict[str, Any] = {
            "headless": headless,
            "accept_downloads": True,
            "downloads_path": str(output_dir),
            "offline": True,
            "proxy": {
                "server": connect_proxy.server_url,
                "bypass": "<-loopback>",
            },
            "service_workers": "block",
            "args": [
                "--disable-background-networking",
                "--disable-blink-features=AutomationControlled",
                "--disable-component-update",
                "--disable-default-apps",
                "--disable-extensions",
                "--disable-quic",
                "--disable-sync",
                "--dns-prefetch-disable",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--no-first-run",
            ],
        }
        if channel and channel.casefold() != "chromium":
            launch_options["channel"] = channel
        browser = await p.chromium.launch_persistent_context(
            str(profile_dir), **launch_options
        )
        try:
            await install_browser_network_policy(
                browser,
                network_audit,
                resolve_target=resolve_target,
            )
            for existing_page in list(browser.pages):
                await existing_page.close()
            await browser.set_offline(False)
            page = await browser.new_page()
            page.set_default_timeout(timeout_seconds * 1000)
            await page.goto(
                url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000
            )
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except PlaywrightTimeoutError:
                pass
            candidates = await visible_download_candidates(page)
            download = await click_download_candidate(
                page, timeout_seconds=timeout_seconds
            )
            mode = "auto_click"
            if download is None and manual_timeout_seconds > 0 and not headless:
                print(
                    "Automatic ResearchGate click did not produce a download. "
                    "Use the open browser window to click the PDF download button now...",
                    file=sys.stderr,
                    flush=True,
                )
                try:
                    async with page.expect_download(
                        timeout=manual_timeout_seconds * 1000
                    ) as download_info:
                        await page.bring_to_front()
                    download = await download_info.value
                    mode = "manual_click_captured"
                except PlaywrightTimeoutError:
                    download = None
            if download is None:
                return {
                    "ok": False,
                    "status": "download_not_triggered",
                    "url": redact_network_url(url),
                    "candidates": candidates,
                    "page_url": redact_network_url(str(page.url or "")),
                    "title": await safe_title(page),
                    "network_policy": network_audit.to_dict(),
                }
            saved = await save_download(
                download,
                output_dir=output_dir,
                target_prefix=target_prefix,
                max_pdf_bytes=max(1, int(max_pdf_bytes)),
            )
            if isinstance(saved, dict):
                saved_fields = saved
                saved_ok = saved.get("ok")
            else:
                saved_fields = {"save_result_type": type(saved).__name__}
                saved_ok = None
            status = (
                "downloaded"
                if saved_ok is True
                else "download_not_pdf"
                if saved_ok is False
                else "download_invalid_result"
            )
            result = {
                **saved_fields,
                "ok": saved_ok is True,
                "status": status,
                "mode": mode,
                "url": redact_network_url(url),
                "page_url": redact_network_url(str(page.url or "")),
                "title": await safe_title(page),
                "candidates": candidates,
                "network_policy": network_audit.to_dict(),
            }
            return result
        except PlaywrightError as exc:
            return {
                "ok": False,
                "status": (
                    "network_policy_blocked"
                    if network_audit.blocked_navigation
                    else "browser_error"
                ),
                "url": initial_decision.to_audit_dict()["url"],
                "error": str(exc),
                "network_policy": network_audit.to_dict(),
            }
        finally:
            if keep_open and not headless:
                print(
                    "Browser left open because --keep-open was passed. Press Ctrl+C in this shell when done.",
                    file=sys.stderr,
                )
                while True:
                    await asyncio.sleep(3600)
            await browser.close()


async def visible_download_candidates(page: Any) -> list[dict[str, str]]:
    raw_candidates = await page.evaluate(
        """
        () => Array.from(document.querySelectorAll('a,button,[role="button"]'))
          .map((node) => ({
            tag: node.tagName.toLowerCase(),
            text: (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim(),
            href: node.href || node.getAttribute('href') || '',
            aria: node.getAttribute('aria-label') || '',
            title: node.getAttribute('title') || ''
          }))
          .filter((item) => /download|full\\s*text|pdf/i.test([item.text, item.href, item.aria, item.title].join(' ')))
          .slice(0, 30)
        """
    )
    if not isinstance(raw_candidates, list):
        return []
    fields = ("tag", "text", "href", "aria", "title")
    candidates: list[dict[str, str]] = []
    for item in raw_candidates[:30]:
        if not isinstance(item, dict):
            continue
        candidate: dict[str, str] = {}
        for field in fields:
            value = item.get(field)
            if not isinstance(value, str):
                break
            if field == "href":
                candidate[field] = redact_network_url(value) if value else ""
            else:
                candidate[field] = value[:500]
        else:
            candidates.append(candidate)
    return candidates


async def click_download_candidate(page: Any, *, timeout_seconds: int) -> Any | None:
    locators = [
        page.get_by_role("link", name=DOWNLOAD_TEXT_RE),
        page.get_by_role("button", name=DOWNLOAD_TEXT_RE),
        page.locator("a[href*='.pdf']"),
        page.locator("a[href*='/publication/'][href*='/links/']"),
        page.get_by_text(DOWNLOAD_TEXT_RE),
    ]
    for locator in locators:
        try:
            if await locator.count() < 1:
                continue
            candidate = locator.first
            if not await candidate.is_visible(timeout=1500):
                continue
            async with page.expect_download(
                timeout=timeout_seconds * 1000
            ) as download_info:
                await candidate.click()
            return await download_info.value
        except Exception:
            continue
    return None


async def save_download(
    download: Any,
    *,
    output_dir: Path,
    target_prefix: str,
    max_pdf_bytes: int = DEFAULT_MAX_PDF_BYTES,
) -> dict[str, Any]:
    prefix = safe_filename_part(target_prefix, max_chars=64)
    suggested = safe_filename_part(
        Path(download.suggested_filename or "researchgate.pdf").stem,
        max_chars=96,
    )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = output_dir / f"{prefix}_{stamp}_{uuid.uuid4().hex}_{suggested}.pdf"
    owner_identity: tuple[int, int] | None = None
    try:
        await download.save_as(str(target))
        owner_identity = _download_artifact_identity(target)
        if owner_identity is None:
            return {
                "ok": False,
                "output_path": str(target),
                "suggested_filename": download.suggested_filename,
                "reason": "downloaded_artifact_is_not_owned_regular_file",
                "removed": False,
            }
        size = target.stat().st_size
        if size > max(1, int(max_pdf_bytes)):
            removed, cleanup_error = _remove_download_artifact(
                target,
                expected_identity=owner_identity,
            )
            result = {
                "ok": False,
                "output_path": str(target),
                "suggested_filename": download.suggested_filename,
                "size": size,
                "reason": "downloaded_pdf_exceeds_size_limit",
                "max_pdf_bytes": max(1, int(max_pdf_bytes)),
                "removed": removed,
            }
            if cleanup_error is not None:
                result["cleanup_error"] = cleanup_error
            return result
        with target.open("rb") as handle:
            is_pdf = handle.read(5) == b"%PDF-"
        if _download_artifact_identity(target) != owner_identity:
            return {
                "ok": False,
                "output_path": str(target),
                "suggested_filename": download.suggested_filename,
                "size": size,
                "reason": "downloaded_artifact_ownership_changed",
                "removed": False,
            }
        if not is_pdf:
            removed, cleanup_error = _remove_download_artifact(
                target,
                expected_identity=owner_identity,
            )
            result = {
                "ok": False,
                "output_path": str(target),
                "suggested_filename": download.suggested_filename,
                "size": size,
                "reason": "downloaded_file_is_not_pdf",
                "removed": removed,
            }
            if cleanup_error is not None:
                result["cleanup_error"] = cleanup_error
            return result
        return {
            "ok": True,
            "output_path": str(target),
            "suggested_filename": download.suggested_filename,
            "size": size,
        }
    except BaseException as exc:
        cleanup_identity = owner_identity or _download_artifact_identity(target)
        removed, cleanup_error = _remove_download_artifact(
            target,
            expected_identity=cleanup_identity,
        )
        if not removed:
            exc.add_note(
                "ResearchGate partial download cleanup failed for "
                f"{target}: {cleanup_error or 'artifact remains'}"
            )
        raise


def _download_artifact_identity(path: Path) -> tuple[int, int] | None:
    try:
        stat_result = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(stat_result.st_mode):
        return None
    return int(stat_result.st_dev), int(stat_result.st_ino)


def _remove_download_artifact(
    path: Path,
    *,
    expected_identity: tuple[int, int] | None,
) -> tuple[bool, str | None]:
    current_identity = _download_artifact_identity(path)
    if current_identity is None:
        try:
            path.lstat()
        except FileNotFoundError:
            return True, None
        except OSError as exc:
            return False, f"{type(exc).__name__}: {exc}"[:500]
        return False, "download artifact is not a regular file"
    if expected_identity is None or current_identity != expected_identity:
        return False, "download artifact ownership changed"
    try:
        path.unlink()
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"[:500]
    try:
        path.lstat()
    except FileNotFoundError:
        return True, None
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"[:500]
    return False, f"Download artifact still exists after cleanup: {path}"[:500]


def attach_pdf_to_zotero_parent(
    config: WorkerConfig,
    *,
    item_key: str,
    source_path: Path,
    data_dir: str,
    force: bool,
) -> dict[str, Any]:
    metadata, store = find_item(config, item_key=item_key, data_dir=data_dir)
    inventory = store.item_full_text_inventory(metadata)
    if inventory.get("has_pdf") is True and not force:
        return {
            "ok": True,
            "skipped": True,
            "status": "parent_already_has_pdf",
            "item_key": item_key,
            "inventory": inventory,
        }
    attachment = synthetic_attachment_for_item(store=store, metadata=metadata)
    filename = f"{safe_filename_part(metadata.title or item_key)} [FULL TEXT].pdf"
    try:
        expected_source = _stable_file_fingerprint(
            source_path,
            max_bytes=DEFAULT_MAX_PDF_BYTES,
        )
        snapshot = _create_parent_attachment_source_snapshot(
            source_path,
            expected_source=expected_source,
            max_bytes=DEFAULT_MAX_PDF_BYTES,
        )
    except OSError as exc:
        return {
            "ok": False,
            "status": "attachment_snapshot_failed",
            "item_key": item_key,
            "source_path": str(source_path),
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }
    try:
        relay_source_path = shared_relay_path(snapshot.path)
        relay_result = create_parent_pdf_attachment(
            ZoteroRelayClient(config),
            metadata=metadata,
            source_path=snapshot.path,
            relay_source_path=relay_source_path,
            filename=filename,
            title=f"{metadata.title or filename} [full text]",
            probe_attachment_key=inventory_probe_attachment_key(inventory),
        )
        if not isinstance(relay_result, dict) or relay_result.get("ok") is not True:
            return {
                "ok": False,
                "status": "relay_attachment_invalid_result",
                "item_key": item_key,
                "source_path": str(source_path),
                "relay_source_path": str(relay_source_path),
                "relay": relay_result,
            }
        local_copy = write_parent_attachment_local_copy(
            attachment=attachment,
            source_path=snapshot.path,
            filename=filename,
            relay_result=relay_result,
            expected_source=snapshot.fingerprint,
            max_source_bytes=DEFAULT_MAX_PDF_BYTES,
        )
        if not isinstance(local_copy, dict) or local_copy.get("ok") is not True:
            return {
                "ok": False,
                "status": "local_copy_invalid_result",
                "item_key": item_key,
                "source_path": str(source_path),
                "relay_source_path": str(relay_source_path),
                "relay": relay_result,
                "local_copy": local_copy,
            }
        local_metadata = sync_parent_attachment_local(
            metadata=metadata,
            attachment=attachment,
            filename=filename,
            title=f"{metadata.title or filename} [full text]",
            content_type="application/pdf",
            relay_result=relay_result,
        )
        if not isinstance(local_metadata, dict) or local_metadata.get("ok") is not True:
            return {
                "ok": False,
                "status": "local_metadata_invalid_result",
                "item_key": item_key,
                "source_path": str(source_path),
                "relay_source_path": str(relay_source_path),
                "relay": relay_result,
                "local_copy": local_copy,
                "local_metadata": local_metadata,
            }
        return {
            "ok": True,
            "status": "attached",
            "item_key": item_key,
            "library_id": metadata.library_id,
            "data_dir": str(metadata.data_dir),
            "source_path": str(source_path),
            "source_sha256": expected_source.sha256,
            "relay_source_path": str(relay_source_path),
            "filename": filename,
            "relay": relay_result,
            "local_copy": local_copy,
            "local_metadata": local_metadata,
        }
    finally:
        snapshot.close()


def create_parent_pdf_attachment(
    relay: ZoteroRelayClient,
    *,
    metadata: LocalItemMetadata,
    source_path: Path,
    relay_source_path: str,
    filename: str,
    title: str,
    probe_attachment_key: str | None,
) -> dict[str, Any]:
    source_fingerprint = _stable_file_fingerprint(
        source_path, max_bytes=DEFAULT_MAX_PDF_BYTES
    )
    payload = {
        "sourcePath": relay_source_path,
        "filename": filename,
        "title": title,
        "contentType": "application/pdf",
        "libraryId": metadata.library_id,
        "probeAttachmentKey": probe_attachment_key or "",
        "deduplicationKey": f"researchgate-pdf:{metadata.library_id}:{metadata.key}:sha256:{source_fingerprint.sha256}",
        "sourceSha256": source_fingerprint.sha256,
    }
    return relay.request_json(
        method="POST",
        path=f"/attachments/parents/{urllib.parse.quote(metadata.key, safe='')}/attachments/file",
        payload=payload,
        error_label="zotero-file-relay ResearchGate PDF attachment",
    )


def find_item(
    config: WorkerConfig, *, item_key: str, data_dir: str
) -> tuple[LocalItemMetadata, LocalZoteroStore]:
    data_dirs = (
        [config.translate_zotero_input_path(data_dir)]
        if data_dir
        else list(config.zotero_data_dirs)
    )
    for candidate_data_dir in data_dirs:
        library_config = replace(
            config,
            zotero_data_dir=candidate_data_dir,
            zotero_data_dirs=(candidate_data_dir,),
            zotero_storage_dir=None,
        )
        store = LocalZoteroStore(library_config)
        try:
            return store.get_item_metadata(item_key), store
        except FileNotFoundError:
            continue
    raise FileNotFoundError(f"Zotero parent item was not found: {item_key}")


def synthetic_attachment_for_item(
    *, store: LocalZoteroStore, metadata: LocalItemMetadata
) -> LocalAttachment:
    return LocalAttachment(
        library_id=metadata.library_id,
        data_dir=metadata.data_dir,
        storage_dir=store.config.resolved_storage_dir,
        key=metadata.key,
        item_id=None,
        parent_item_id=metadata.item_id,
        date_modified=metadata.date_modified,
        link_mode=None,
        content_type=None,
        zotero_path=None,
        file_path=Path(f"{safe_filename_part(metadata.title or metadata.key)}.pdf"),
        parent_key=metadata.key,
    )


def inventory_probe_attachment_key(inventory: dict[str, object]) -> str | None:
    attachments = inventory.get("attachments")
    if not isinstance(attachments, list):
        return None
    for item in attachments:
        if not isinstance(item, dict):
            continue
        value = item.get("key")
        if not isinstance(value, str):
            continue
        key = value.strip()
        if re.fullmatch(r"[A-Za-z0-9]{1,64}", key) is None:
            continue
        if key:
            return key
    return None


def shared_relay_path(source_path: Path) -> str:
    resolved = source_path.resolve()
    mappings = [
        (PROJECT_ROOT / "data" / "ingest", "/data/ingest"),
        (PROJECT_ROOT / "data" / "html", "/data/html"),
    ]
    for host_root, relay_root in mappings:
        try:
            relative = resolved.relative_to(host_root.resolve())
        except ValueError:
            continue
        return f"{relay_root}/{relative.as_posix()}"
    return str(source_path)


async def safe_title(page: Any) -> str:
    try:
        title = await page.title()
        return title[:500] if isinstance(title, str) else ""
    except Exception:
        return ""


def safe_filename_part(value: str, *, max_chars: int = 120) -> str:
    return safe_filename_component(value, default="researchgate", max_chars=max_chars)


if __name__ == "__main__":
    raise SystemExit(main())
