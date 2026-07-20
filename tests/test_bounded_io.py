from __future__ import annotations

from pathlib import Path

import pytest

from zotero_ingest_worker.bounded_io import read_bytes_bounded, read_text_bounded


def test_bounded_reader_accepts_exact_budget(tmp_path: Path) -> None:
    path = tmp_path / "value.txt"
    path.write_bytes("test".encode("utf-8"))

    assert read_bytes_bounded(path, max_bytes=4) == b"test"
    assert read_text_bounded(path, max_bytes=4) == "test"


def test_bounded_reader_rejects_oversized_file(tmp_path: Path) -> None:
    path = tmp_path / "value.txt"
    path.write_bytes(b"12345")

    with pytest.raises(OSError, match="exceeds 4 bytes"):
        read_bytes_bounded(path, max_bytes=4)


@pytest.mark.parametrize("value", [True, -1, 1.5, "4", None])
def test_bounded_reader_rejects_invalid_budget(
    tmp_path: Path,
    value: object,
) -> None:
    path = tmp_path / "value.txt"
    path.write_bytes(b"test")

    with pytest.raises(ValueError, match="max_bytes"):
        read_bytes_bounded(path, max_bytes=value)  # type: ignore[arg-type]
