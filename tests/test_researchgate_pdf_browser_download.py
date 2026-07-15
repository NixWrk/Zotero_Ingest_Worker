from __future__ import annotations

import asyncio
import importlib.util
import sys
from types import ModuleType
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


def test_private_initial_url_is_blocked_before_playwright_or_filesystem(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    profile_dir = tmp_path / "profile"

    result = asyncio.run(
        rg.download_researchgate_pdf(
            url="https://www.researchgate.net/publication/example",
            output_dir=output_dir,
            profile_dir=profile_dir,
            item_key="ITEM1",
            channel="chromium",
            headless=True,
            timeout_seconds=1,
            manual_timeout_seconds=0,
            keep_open=False,
            resolve_target=lambda _url: [SimpleNamespace(ip="127.0.0.1")],
        )
    )

    assert result["ok"] is False
    assert result["status"] == "unsafe_browser_url"
    assert result["reason"] == "blocked_resolved_address"
    assert result["network_policy"]["blocked_navigation"] == 1
    assert not output_dir.exists()
    assert not profile_dir.exists()


def test_browser_policy_is_installed_before_new_page_and_navigation(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    launch_options: dict[str, Any] = {}

    class FakePlaywrightError(Exception):
        pass

    class FakePlaywrightTimeoutError(FakePlaywrightError):
        pass

    class FakePage:
        url = "https://www.researchgate.net/publication/example"

        def set_default_timeout(self, _timeout: int) -> None:
            events.append("set_timeout")

        async def close(self) -> None:
            events.append("close_existing_page")

        async def goto(self, *_args: Any, **_kwargs: Any) -> None:
            events.append("goto")

        async def wait_for_load_state(self, *_args: Any, **_kwargs: Any) -> None:
            events.append("networkidle")

        async def title(self) -> str:
            return "Paper"

    class FakeContext:
        def __init__(self) -> None:
            self.pages = [FakePage()]

        async def add_init_script(self, *, script: str) -> None:
            assert "WebSocket" in script
            events.append("init_script")

        async def route_web_socket(self, pattern: str, _handler: Any) -> None:
            assert pattern == "**/*"
            events.append("websocket_route")

        async def route(self, pattern: str, _handler: Any) -> None:
            assert pattern == "**/*"
            events.append("route")

        async def new_page(self) -> FakePage:
            events.append("new_page")
            return FakePage()

        async def close(self) -> None:
            events.append("close_context")

    context = FakeContext()

    class FakeChromium:
        async def launch_persistent_context(
            self,
            _profile: str,
            **kwargs: Any,
        ) -> FakeContext:
            launch_options.update(kwargs)
            events.append("launch")
            return context

    class FakeManager:
        async def __aenter__(self) -> SimpleNamespace:
            return SimpleNamespace(chromium=FakeChromium())

        async def __aexit__(self, *_args: Any) -> None:
            return None

    fake_playwright = ModuleType("playwright")
    fake_async_api = ModuleType("playwright.async_api")
    fake_async_api.Error = FakePlaywrightError
    fake_async_api.TimeoutError = FakePlaywrightTimeoutError
    fake_async_api.async_playwright = lambda: FakeManager()
    monkeypatch.setitem(sys.modules, "playwright", fake_playwright)
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_async_api)

    async def fake_candidates(_page: Any) -> list[dict[str, str]]:
        return []

    async def fake_click(_page: Any, *, timeout_seconds: int) -> SimpleNamespace:
        assert timeout_seconds == 1
        return SimpleNamespace()

    async def fake_save(
        _download: Any,
        *,
        output_dir: Path,
        target_prefix: str,
        max_pdf_bytes: int,
    ) -> dict[str, Any]:
        assert output_dir == tmp_path / "output"
        assert target_prefix == "ITEM1"
        assert max_pdf_bytes == rg.DEFAULT_MAX_PDF_BYTES
        return {"ok": True, "output_path": str(output_dir / "paper.pdf"), "size": 10}

    monkeypatch.setattr(rg, "visible_download_candidates", fake_candidates)
    monkeypatch.setattr(rg, "click_download_candidate", fake_click)
    monkeypatch.setattr(rg, "save_download", fake_save)

    result = asyncio.run(
        rg.download_researchgate_pdf(
            url="https://www.researchgate.net/publication/example",
            output_dir=tmp_path / "output",
            profile_dir=tmp_path / "profile",
            item_key="ITEM1",
            channel="chromium",
            headless=True,
            timeout_seconds=1,
            manual_timeout_seconds=0,
            keep_open=False,
            resolve_target=lambda _url: [SimpleNamespace(ip="93.184.216.34")],
        )
    )

    assert result["ok"] is True
    assert launch_options["service_workers"] == "block"
    assert events.index("init_script") < events.index("new_page")
    assert events.index("websocket_route") < events.index("new_page")
    assert events.index("route") < events.index("new_page") < events.index("goto")
    assert events.index("route") < events.index("close_existing_page")


def test_download_candidate_uses_playwright_first_property() -> None:
    class EmptyLocator:
        @property
        def first(self) -> EmptyLocator:
            return self

        async def count(self) -> int:
            return 0

    class FakePage:
        def get_by_role(self, *_args: Any, **_kwargs: Any) -> EmptyLocator:
            return EmptyLocator()

        def locator(self, *_args: Any, **_kwargs: Any) -> EmptyLocator:
            return EmptyLocator()

        def get_by_text(self, *_args: Any, **_kwargs: Any) -> EmptyLocator:
            return EmptyLocator()

    result = asyncio.run(rg.click_download_candidate(FakePage(), timeout_seconds=1))

    assert result is None


def test_save_download_rejects_and_removes_oversized_pdf(tmp_path: Path) -> None:
    class FakeDownload:
        suggested_filename = "paper.pdf"

        async def save_as(self, target: str) -> None:
            Path(target).write_bytes(b"%PDF-" + (b"x" * 20))

    result = asyncio.run(
        rg.save_download(
            FakeDownload(),
            output_dir=tmp_path,
            target_prefix="ITEM1",
            max_pdf_bytes=10,
        )
    )

    assert result["ok"] is False
    assert result["reason"] == "downloaded_pdf_exceeds_size_limit"
    assert result["removed"] is True
    assert not Path(result["output_path"]).exists()


def test_save_download_accepts_pdf_magic_with_bounded_header_read(tmp_path: Path) -> None:
    class FakeDownload:
        suggested_filename = "paper.pdf"

        async def save_as(self, target: str) -> None:
            Path(target).write_bytes(b"%PDF-test")

    result = asyncio.run(
        rg.save_download(
            FakeDownload(),
            output_dir=tmp_path,
            target_prefix="ITEM1",
            max_pdf_bytes=100,
        )
    )

    assert result["ok"] is True
    assert result["size"] == 9
