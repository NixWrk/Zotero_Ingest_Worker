from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path


def library_id_for_data_dir(data_dir: Path) -> str:
    binding_id = _zotero_library_id_for_data_dir(data_dir)
    if binding_id:
        return binding_id
    return path_library_id_for_data_dir(data_dir)


def path_library_id_for_data_dir(data_dir: Path) -> str:
    resolved = str(data_dir.resolve()).lower()
    digest = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:8]
    label = re.sub(r"[^A-Za-z0-9]+", "_", data_dir.name).strip("_") or "zotero"
    return f"{label}_{digest}"


def _zotero_library_id_for_data_dir(data_dir: Path) -> str:
    for binding in _zfr_library_bindings_from_env():
        library_id = str(binding.get("zoteroLibraryId") or "").strip()
        if not library_id:
            continue
        for key in ("hostDataDir", "dataDir"):
            raw_path = str(binding.get(key) or "").strip()
            if raw_path and _same_path(Path(raw_path), data_dir):
                return library_id
    return ""


def _zfr_library_bindings_from_env() -> tuple[dict[str, object], ...]:
    value = os.environ.get("ZFR_LIBRARY_BINDINGS", "").strip()
    if not value:
        return ()
    bindings = json.loads(value)
    if not isinstance(bindings, list):
        raise ValueError("Invalid ZFR_LIBRARY_BINDINGS: expected a JSON list.")
    return tuple(binding for binding in bindings if isinstance(binding, dict))


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.expanduser().resolve() == right.expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return _normalize_path_text(str(left)) == _normalize_path_text(str(right))


def _normalize_path_text(value: str) -> str:
    return value.replace("\\", "/").rstrip("/").casefold()


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
