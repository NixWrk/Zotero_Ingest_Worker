from __future__ import annotations

import os
from pathlib import Path


def provider_script_path(
    filename: str,
    *,
    package_root: Path | None = None,
    cwd: Path | None = None,
) -> Path:
    package_root = package_root or Path(__file__).resolve().parents[1]
    cwd = cwd or Path.cwd()
    relative = Path("scripts") / "providers" / filename
    candidates: list[Path] = []
    env_root = os.environ.get("ZOTERO_INGEST_PROJECT_ROOT", "").strip()
    if env_root:
        candidates.append(Path(env_root) / relative)
    candidates.extend(
        [
            package_root / relative,
            cwd / relative,
            Path("/app") / relative,
        ]
    )
    for candidate in _unique_paths(candidates):
        if candidate.exists():
            return candidate
    return candidates[0] if candidates else package_root / relative


def _unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result
