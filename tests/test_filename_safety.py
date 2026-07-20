from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

import zotero_ingest_worker.arxiv_html as arxiv_html_module
import zotero_ingest_worker.full_text_attachment as full_text_attachment_module
import zotero_ingest_worker.full_text_discovery as full_text_discovery_module
import zotero_ingest_worker.metadata_processor_helpers as metadata_helpers_module
from zotero_ingest_worker.filename_safety import safe_filename_component


SANITIZERS: tuple[tuple[str, Callable[[str], str], int], ...] = (
    ("metadata", metadata_helpers_module._safe_filename, 160),
    ("arxiv", arxiv_html_module.safe_filename, 160),
    ("attachment", full_text_attachment_module._safe_filename, 180),
    ("discovery", full_text_discovery_module._safe_filename, 160),
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("CON", "_CON"),
        ("con.txt", "_con.txt"),
        ("PRN.", "_PRN"),
        ("AUX ", "_AUX"),
        ("NUL.html", "_NUL.html"),
        ("COM1", "_COM1"),
        ("com9.pdf", "_com9.pdf"),
        ("LPT1", "_LPT1"),
        ("lpt9.txt", "_lpt9.txt"),
        ("COM¹.txt", "_COM¹.txt"),
        ("LPT³", "_LPT³"),
        ("CLOCK$", "_CLOCK$"),
        ("CONIN$.txt", "_CONIN$.txt"),
        ("CONOUT$", "_CONOUT$"),
        ("ordinary paper", "ordinary paper"),
    ],
)
@pytest.mark.parametrize(("_name", "sanitize", "_max_chars"), SANITIZERS)
def test_ingest_filename_sanitizers_avoid_windows_device_names(
    _name: str,
    sanitize: Callable[[str], str],
    _max_chars: int,
    value: str,
    expected: str,
) -> None:
    assert sanitize(value) == expected


@pytest.mark.parametrize(("_name", "sanitize", "max_chars"), SANITIZERS)
def test_ingest_filename_sanitizers_preserve_component_contract(
    _name: str,
    sanitize: Callable[[str], str],
    max_chars: int,
) -> None:
    assert sanitize("  paper<>title.  ") == "paper_title"
    assert sanitize("...") == "document"
    assert len(sanitize("x" * 500)) == max_chars


@pytest.mark.parametrize("max_chars", [True, False, 0, -1, 1.5, "3", None])
def test_shared_filename_sanitizer_rejects_invalid_limits(max_chars: Any) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        safe_filename_component(
            "paper",
            default="document",
            max_chars=max_chars,
        )


def test_shared_filename_sanitizer_checks_fallback_and_truncated_value() -> None:
    assert (
        safe_filename_component(
            "",
            default="CON.txt",
            max_chars=160,
        )
        == "_CON.txt"
    )
    assert (
        safe_filename_component(
            "CON.suffix",
            default="document",
            max_chars=3,
        )
        == "_CO"
    )
    assert safe_filename_component("...", default="...", max_chars=1) == "d"


WINDOWS_RESERVED_STEMS = tuple(
    sorted(
        {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"}
        | {
            f"{prefix}{suffix}"
            for prefix in ("COM", "LPT")
            for suffix in (*map(str, range(1, 10)), "\u00b9", "\u00b2", "\u00b3")
        }
    )
)


@pytest.mark.parametrize("stem", WINDOWS_RESERVED_STEMS)
@pytest.mark.parametrize(("_name", "sanitize", "_max_chars"), SANITIZERS)
def test_ingest_filename_sanitizers_cover_entire_windows_device_namespace(
    _name: str,
    sanitize: Callable[[str], str],
    _max_chars: int,
    stem: str,
) -> None:
    for value in (stem, f"{stem.lower()}.txt"):
        assert sanitize(value) == f"_{value}"


@pytest.mark.parametrize("stem", WINDOWS_RESERVED_STEMS)
def test_shared_filename_sanitizer_rechecks_every_device_name_after_truncation(
    stem: str,
) -> None:
    result = safe_filename_component(
        f"{stem}.suffix",
        default="document",
        max_chars=len(stem),
    )

    assert result.startswith("_")
    assert len(result) == len(stem)
