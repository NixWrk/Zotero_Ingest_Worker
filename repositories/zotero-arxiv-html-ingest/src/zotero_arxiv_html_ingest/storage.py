from __future__ import annotations

import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import ArxivCandidate, ArxivHtmlArtifact, LocalAttachment

_WINDOWS_RESERVED_FILENAME_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"}
    | {
        f"{prefix}{suffix}"
        for prefix in ("COM", "LPT")
        for suffix in (*map(str, range(1, 10)), "\u00b9", "\u00b2", "\u00b3")
    }
)


def write_arxiv_html_artifact(
    *,
    root: Path,
    attachment: LocalAttachment,
    candidate: ArxivCandidate,
    html_text: str,
    validation: dict[str, Any],
    source_pdf: Path | None = None,
) -> ArxivHtmlArtifact:
    source = source_pdf or attachment.file_path
    signature = file_signature(source)
    stem = safe_filename(
        Path(attachment.filename).stem or candidate.arxiv_id or "article"
    )
    target_dir = root / attachment.library_id / attachment.key / signature / stem
    target_dir.mkdir(parents=True, exist_ok=True)
    output_path = target_dir / arxiv_html_filename(attachment.filename)
    output_path.write_text(html_text, encoding="utf-8")
    html_url = f"https://arxiv.org/html/{candidate.arxiv_id}"
    manifest = {
        "job_kind": "arxiv_html",
        "library_id": attachment.library_id,
        "attachment_key": attachment.key,
        "source_pdf": str(source),
        "arxiv_id": candidate.arxiv_id,
        "html_url": html_url,
        "candidate": candidate.to_dict(),
        "validation": validation,
        "output": str(output_path),
        "created_at": datetime.now(UTC).isoformat(),
    }
    manifest_path = target_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return ArxivHtmlArtifact(
        path=output_path,
        manifest_path=manifest_path,
        candidate=candidate,
        html_url=html_url,
        validation=validation,
    )


def arxiv_html_filename(pdf_filename: str) -> str:
    stem = Path(pdf_filename).stem or "document"
    stem = re.sub(r"\s+\[arxiv html\]$", "", stem, flags=re.IGNORECASE)
    return f"{stem} [ARXIV HTML].html"


def copy_relay_sibling_local(
    *,
    attachment: LocalAttachment,
    source_path: Path,
    filename: str,
    relay_result: dict[str, Any],
) -> dict[str, Any]:
    sibling_key = str(
        relay_result.get("siblingKey") or relay_result.get("newAttachmentKey") or ""
    ).strip()
    if not sibling_key:
        raise RuntimeError("Relay result did not include siblingKey.")
    target_dir = attachment.storage_dir / sibling_key
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / Path(filename).name
    temp_path = target_dir / f".{target_path.name}.html-tmp"
    shutil.copy2(source_path, temp_path)
    temp_path.replace(target_path)
    return {"ok": True, "siblingKey": sibling_key, "path": str(target_path)}


def file_signature(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_size}_{stat.st_mtime_ns}"


def safe_filename(value: str) -> str:
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", str(value or "document"))
    value = re.sub(r"\s+", " ", value).strip(" .") or "document"
    candidate = value[:160].rstrip(" .") or "document"
    stem = candidate.split(".", 1)[0].rstrip(" .").upper()
    if stem in _WINDOWS_RESERVED_FILENAME_STEMS:
        candidate = f"_{candidate}"[:160].rstrip(" .")
    return candidate or "_"
