from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "providers" / "researchgate_pdf_browser_download.py"
SPEC = importlib.util.spec_from_file_location("researchgate_pdf_browser_download", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
rg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rg)


def test_shared_relay_path_maps_ingest_data_dir() -> None:
    source = rg.PROJECT_ROOT / "data" / "ingest" / "researchgate_browser_downloads" / "paper.pdf"

    assert rg.shared_relay_path(source) == "/data/ingest/researchgate_browser_downloads/paper.pdf"


def test_create_parent_pdf_attachment_uses_relay_visible_source_path(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF- test")
    captured: dict[str, Any] = {}

    class FakeRelay:
        def request_json(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"ok": True, "newAttachmentKey": "PDF1234"}

    metadata = SimpleNamespace(
        library_id="LIB1",
        key="ITEM 1",
    )

    result = rg.create_parent_pdf_attachment(
        FakeRelay(),
        metadata=metadata,
        source_path=source,
        relay_source_path="/data/ingest/researchgate/paper.pdf",
        filename="paper [FULL TEXT].pdf",
        title="Paper [full text]",
        probe_attachment_key="OLDHTML1",
    )

    assert result["newAttachmentKey"] == "PDF1234"
    assert captured["path"] == "/attachments/parents/ITEM%201/attachments/file"
    payload = captured["payload"]
    assert payload["sourcePath"] == "/data/ingest/researchgate/paper.pdf"
    assert payload["contentType"] == "application/pdf"
    assert payload["probeAttachmentKey"] == "OLDHTML1"
    assert payload["deduplicationKey"].startswith("researchgate-pdf:LIB1:ITEM 1:")


def test_run_skips_browser_download_when_parent_already_has_pdf(monkeypatch: Any) -> None:
    metadata = SimpleNamespace(key="ITEM1")
    store = SimpleNamespace(item_full_text_inventory=lambda _metadata: {"has_pdf": True, "attachments": []})

    async def unexpected_download(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("ResearchGate browser download should not start when a PDF already exists.")

    monkeypatch.setattr(rg, "from_env", lambda: SimpleNamespace())
    monkeypatch.setattr(rg, "find_item", lambda *_args, **_kwargs: (metadata, store))
    monkeypatch.setattr(rg, "download_researchgate_pdf", unexpected_download)

    result = asyncio.run(
        rg.run(
            SimpleNamespace(
                url="https://www.researchgate.net/publication/example",
                item_key="ITEM1",
                data_dir="",
                output_dir="",
                profile_dir="",
                channel="msedge",
                headless=False,
                timeout_seconds=1,
                manual_timeout_seconds=0,
                keep_open=False,
                attach=True,
                force_attach=False,
            )
        )
    )

    assert result["ok"] is True
    assert result["status"] == "parent_already_has_pdf"
    assert result["download"]["skipped"] is True
    assert result["attach"]["skipped"] is True
