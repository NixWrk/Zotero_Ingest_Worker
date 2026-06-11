from __future__ import annotations

import json
from pathlib import Path

from zotero_ingest_worker.local_attachment_sync import (
    patched_sync_cache_json,
    replace_original_file,
)


def test_patched_sync_cache_json_updates_nested_file_metadata() -> None:
    raw = json.dumps(
        {
            "key": "PDF1234",
            "version": 7,
            "data": {"key": "PDF1234", "version": 7, "md5": "old", "mtime": 1000},
        }
    )

    patched = patched_sync_cache_json(
        raw,
        attachment_key="PDF1234",
        version=8,
        storage_hash="new-md5",
        storage_mtime=2000,
    )

    payload = json.loads(str(patched))
    assert payload["version"] == 8
    assert payload["data"]["key"] == "PDF1234"
    assert payload["data"]["version"] == 8
    assert payload["data"]["md5"] == "new-md5"
    assert payload["data"]["mtime"] == 2000


def test_replace_original_file_overwrites_pdf(tmp_path: Path) -> None:
    original = tmp_path / "paper.pdf"
    output = tmp_path / "paper.ocr.pdf"
    original.write_bytes(b"old")
    output.write_bytes(b"new")

    replace_original_file(source_path=original, output_pdf=output)

    assert original.read_bytes() == b"new"
    assert not (tmp_path / ".paper.pdf.ocr-tmp").exists()
