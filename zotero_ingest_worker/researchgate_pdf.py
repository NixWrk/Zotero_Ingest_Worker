from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import WorkerConfig
from .provider_scripts import provider_script_path


DownloadResearchGatePdf = Callable[..., Awaitable[dict[str, Any]]]
AttachResearchGatePdf = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class ResearchGatePdfOptions:
    url: str
    item_key: str
    data_dir: str = ""
    output_dir: Path | None = None
    profile_dir: Path | None = None
    channel: str = "msedge"
    headless: bool = True
    timeout_seconds: int = 90
    manual_timeout_seconds: int = 0
    keep_open: bool = False
    force_attach: bool = False


async def download_and_attach_researchgate_pdf(
    config: WorkerConfig,
    options: ResearchGatePdfOptions,
    *,
    download_pdf: DownloadResearchGatePdf | None = None,
    attach_pdf: AttachResearchGatePdf | None = None,
) -> dict[str, Any]:
    module = _script_module()
    preflight = module.preflight_pdf_attach(
        config,
        item_key=options.item_key,
        data_dir=options.data_dir,
        force=options.force_attach,
    )
    if preflight.get("skipped"):
        return {
            "ok": True,
            "status": str(preflight.get("status") or "attach_skipped"),
            "download": {"ok": True, "skipped": True, "reason": preflight.get("status")},
            "attach": preflight,
        }

    download_pdf = download_pdf or module.download_researchgate_pdf
    attach_pdf = attach_pdf or module.attach_pdf_to_zotero_parent
    try:
        download = await download_pdf(
            url=options.url,
            output_dir=options.output_dir or module.DEFAULT_DOWNLOAD_DIR,
            profile_dir=options.profile_dir or module.DEFAULT_PROFILE_DIR,
            item_key=options.item_key,
            channel=options.channel,
            headless=options.headless,
            timeout_seconds=max(1, int(options.timeout_seconds)),
            manual_timeout_seconds=max(0, int(options.manual_timeout_seconds)),
            keep_open=options.keep_open,
        )
    except ModuleNotFoundError as exc:
        if exc.name == "playwright":
            return {
                "ok": False,
                "status": "playwright_missing",
                "error": "Install Playwright first: python -m pip install playwright",
            }
        raise

    payload: dict[str, Any] = {
        "ok": bool(download.get("ok")),
        "status": download.get("status"),
        "download": download,
    }
    if not download.get("ok"):
        return payload

    attach = attach_pdf(
        config,
        item_key=options.item_key,
        source_path=Path(str(download["output_path"])),
        data_dir=options.data_dir,
        force=options.force_attach,
    )
    payload["attach"] = attach
    payload["ok"] = bool(attach.get("ok"))
    payload["status"] = (
        "attached"
        if payload["ok"]
        else str(attach.get("status") or attach.get("reason") or "attach_failed")
    )
    return payload


def _script_module() -> Any:
    script_path = provider_script_path("researchgate_pdf_browser_download.py")
    spec = importlib.util.spec_from_file_location("researchgate_pdf_browser_download", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load ResearchGate browser script: {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
