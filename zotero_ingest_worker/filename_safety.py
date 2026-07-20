from __future__ import annotations

import re


_WINDOWS_RESERVED_FILENAME_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"}
    | {
        f"{prefix}{suffix}"
        for prefix in ("COM", "LPT")
        for suffix in (*map(str, range(1, 10)), "\u00b9", "\u00b2", "\u00b3")
    }
)


def safe_filename_component(
    value: object,
    *,
    default: str,
    max_chars: int,
) -> str:
    if type(max_chars) is not int or max_chars < 1:
        raise ValueError("max_chars must be a positive integer")

    fallback = _clean_filename_component(default) or "document"
    cleaned = _clean_filename_component(str(value or fallback)) or fallback
    candidate = cleaned[:max_chars].rstrip(" .") or fallback[:max_chars].rstrip(" .")
    if _is_windows_reserved_filename(candidate):
        candidate = f"_{candidate}"[:max_chars].rstrip(" .")
    return candidate or "_"


def _clean_filename_component(value: str) -> str:
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", value)
    return re.sub(r"\s+", " ", value).strip(" .")


def _is_windows_reserved_filename(value: str) -> bool:
    stem = value.split(".", 1)[0].rstrip(" .").upper()
    return stem in _WINDOWS_RESERVED_FILENAME_STEMS
