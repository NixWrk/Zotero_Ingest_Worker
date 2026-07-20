from __future__ import annotations

from pathlib import Path


def read_bytes_bounded(path: Path, *, max_bytes: int) -> bytes:
    """Read one local file without allowing it to exceed the memory budget."""
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    try:
        with path.open("rb") as stream:
            payload = stream.read(max_bytes + 1)
    except OSError as exc:
        raise OSError(f"Could not read file: {path}") from exc
    if len(payload) > max_bytes:
        raise OSError(f"File exceeds {max_bytes} bytes: {path}")
    return payload


def read_text_bounded(
    path: Path,
    *,
    max_bytes: int,
    encoding: str = "utf-8",
    errors: str = "strict",
) -> str:
    return read_bytes_bounded(path, max_bytes=max_bytes).decode(
        encoding,
        errors=errors,
    )
