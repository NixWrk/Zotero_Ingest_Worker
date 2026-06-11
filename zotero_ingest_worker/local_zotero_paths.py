from __future__ import annotations

import hashlib
import re
from pathlib import Path


def library_id_for_data_dir(data_dir: Path) -> str:
    resolved = str(data_dir.resolve()).lower()
    digest = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:8]
    label = re.sub(r"[^A-Za-z0-9]+", "_", data_dir.name).strip("_") or "zotero"
    return f"{label}_{digest}"


def safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0


def safe_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def looks_like_generated_html(filename: str) -> bool:
    name = filename.casefold()
    # Only files named by _html_filename() are considered disposable automation output.
    return re.search(r"\[(?:[a-z]{2,12}|mixed|unknown) html\]\.html?$", name) is not None


def resolve_attachment_path_for_suffixes(
    *,
    storage_root: Path,
    key: str,
    zotero_path: str | None,
    suffixes: tuple[str, ...],
    require_exists: bool,
) -> Path | None:
    storage_dir = storage_root / key
    if zotero_path and zotero_path.startswith("storage:"):
        candidate = storage_dir / zotero_path.removeprefix("storage:")
        if not require_exists or safe_exists(candidate):
            return candidate
        return None
    if zotero_path:
        candidate = Path(zotero_path)
        if not require_exists or safe_exists(candidate):
            return candidate
    if safe_exists(storage_dir):
        try:
            files = sorted(
                (
                    child
                    for child in storage_dir.iterdir()
                    if child.is_file() and child.suffix.lower() in suffixes
                ),
                key=safe_mtime,
                reverse=True,
            )
        except OSError:
            return None
        if files:
            return files[0]
    return None
