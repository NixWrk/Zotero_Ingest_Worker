from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import sys
from types import ModuleType
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "providers"
    / "researchgate_pdf_browser_download.py"
)
SPEC = importlib.util.spec_from_file_location(
    "researchgate_pdf_browser_download", SCRIPT_PATH
)
assert SPEC is not None and SPEC.loader is not None
rg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rg)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("CON", "_CON"),
        ("nul.pdf", "_nul.pdf"),
        ("COM¹.txt", "_COM¹.txt"),
        ("ordinary", "ordinary"),
    ],
)
def test_researchgate_filename_part_avoids_windows_device_names(
    value: str,
    expected: str,
) -> None:
    assert rg.safe_filename_part(value) == expected


def test_researchgate_filename_part_remains_safe_after_truncation() -> None:
    assert rg.safe_filename_part("CON.suffix", max_chars=3) == "_CO"


def test_shared_relay_path_maps_ingest_data_dir() -> None:
    source = (
        rg.PROJECT_ROOT
        / "data"
        / "ingest"
        / "researchgate_browser_downloads"
        / "paper.pdf"
    )

    assert (
        rg.shared_relay_path(source)
        == "/data/ingest/researchgate_browser_downloads/paper.pdf"
    )


def test_create_parent_pdf_attachment_uses_relay_visible_source_path(
    tmp_path: Path,
) -> None:
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
    expected_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    assert payload["sourceSha256"] == expected_sha256
    assert payload["deduplicationKey"] == (
        f"researchgate-pdf:LIB1:ITEM 1:sha256:{expected_sha256}"
    )


def test_attach_pdf_uses_pre_relay_snapshot_when_source_mutates(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "paper.pdf"
    original_bytes = b"%PDF-ORIGINAL"
    source.write_bytes(original_bytes)
    metadata = SimpleNamespace(
        library_id="LIB1",
        data_dir=tmp_path,
        key="ITEM1",
        item_id=10,
        title="Paper",
    )
    attachment = SimpleNamespace(storage_dir=tmp_path / "storage")
    store = SimpleNamespace(
        item_full_text_inventory=lambda _metadata: {
            "has_pdf": False,
            "attachments": [],
        }
    )
    monkeypatch.setattr(
        rg,
        "find_item",
        lambda *_args, **_kwargs: (metadata, store),
    )
    monkeypatch.setattr(
        rg,
        "synthetic_attachment_for_item",
        lambda **_kwargs: attachment,
    )

    def mutate_source(_relay: object, **kwargs: Any) -> dict[str, Any]:
        relay_source = Path(kwargs["source_path"])
        assert relay_source != source
        assert relay_source.read_bytes() == original_bytes
        source.write_bytes(b"%PDF-MUTATED")
        return {"ok": True, "newAttachmentKey": "PDF1234"}

    monkeypatch.setattr(rg, "create_parent_pdf_attachment", mutate_source)
    monkeypatch.setattr(
        rg,
        "sync_parent_attachment_local",
        lambda **_kwargs: {"ok": True},
    )

    result = rg.attach_pdf_to_zotero_parent(
        SimpleNamespace(),
        item_key="ITEM1",
        source_path=source,
        data_dir="",
        force=False,
    )

    assert result["ok"] is True
    assert Path(result["local_copy"]["path"]).read_bytes() == original_bytes
    assert source.read_bytes() == b"%PDF-MUTATED"
    assert not list(tmp_path.rglob(".z2m-parent-attachment-snapshot-*"))


def test_run_skips_browser_download_when_parent_already_has_pdf(
    monkeypatch: Any,
) -> None:
    metadata = SimpleNamespace(key="ITEM1")
    store = SimpleNamespace(
        item_full_text_inventory=lambda _metadata: {"has_pdf": True, "attachments": []}
    )

    async def unexpected_download(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError(
            "ResearchGate browser download should not start when a PDF already exists."
        )

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


def test_private_initial_url_is_blocked_before_playwright_or_filesystem(
    tmp_path: Path,
) -> None:
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
        url = "https://www.researchgate.net/publication/example?token=secret"

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

        async def set_offline(self, offline: bool) -> None:
            events.append("set_offline" if offline else "set_online")

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

    saved_ok: object = True

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
        return {
            "ok": saved_ok,
            "output_path": str(output_dir / "paper.pdf"),
            "size": 10,
        }

    monkeypatch.setattr(rg, "visible_download_candidates", fake_candidates)
    monkeypatch.setattr(rg, "click_download_candidate", fake_click)
    monkeypatch.setattr(rg, "save_download", fake_save)

    result = asyncio.run(
        rg.download_researchgate_pdf(
            url="https://www.researchgate.net/publication/example?token=secret",
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
    assert launch_options["offline"] is True
    assert "--disable-extensions" in launch_options["args"]
    assert launch_options["proxy"]["server"].startswith("http://127.0.0.1:")
    assert launch_options["proxy"]["bypass"] == "<-loopback>"
    assert "proxy" not in launch_options["args"]
    assert "secret" not in str(result.get("url") or "")
    assert "secret" not in str(result.get("page_url") or "")
    assert events.index("init_script") < events.index("new_page")
    assert events.index("websocket_route") < events.index("new_page")
    assert events.index("route") < events.index("new_page") < events.index("goto")
    assert events.index("route") < events.index("set_online") < events.index("new_page")
    assert events.index("route") < events.index("close_existing_page")

    saved_ok = "true"
    malformed_result = asyncio.run(
        rg.download_researchgate_pdf(
            url="https://www.researchgate.net/publication/example?token=secret",
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

    assert malformed_result["ok"] is False
    assert malformed_result["status"] == "download_invalid_result"


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


def test_visible_download_candidates_filters_malformed_browser_json() -> None:
    class FakePage:
        async def evaluate(self, _script: str) -> object:
            return [
                {
                    "tag": "a",
                    "text": "Download PDF",
                    "href": "https://example.test/paper.pdf?token=secret",
                    "aria": "",
                    "title": "PDF",
                },
                {
                    "tag": 7,
                    "text": "bad",
                    "href": "",
                    "aria": "",
                    "title": "",
                },
                "not-a-mapping",
            ]

    result = asyncio.run(rg.visible_download_candidates(FakePage()))

    assert result == [
        {
            "tag": "a",
            "text": "Download PDF",
            "href": "https://example.test/paper.pdf?[redacted]",
            "aria": "",
            "title": "PDF",
        }
    ]


def test_safe_title_rejects_non_string_browser_value() -> None:
    page = SimpleNamespace(title=lambda: asyncio.sleep(0, result=123))

    assert asyncio.run(rg.safe_title(page)) == ""


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


def test_save_download_rejects_and_removes_non_pdf_artifact(tmp_path: Path) -> None:
    class FakeDownload:
        suggested_filename = "paper.pdf"

        async def save_as(self, target: str) -> None:
            Path(target).write_bytes(b"<html>login required</html>")

    result = asyncio.run(
        rg.save_download(
            FakeDownload(),
            output_dir=tmp_path,
            target_prefix="ITEM1",
            max_pdf_bytes=100,
        )
    )

    assert result["ok"] is False
    assert result["reason"] == "downloaded_file_is_not_pdf"
    assert result["removed"] is True
    assert not Path(result["output_path"]).exists()


def test_save_download_accepts_pdf_magic_with_bounded_header_read(
    tmp_path: Path,
) -> None:
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


def test_save_download_bounds_windows_filename_component(tmp_path: Path) -> None:
    class FakeDownload:
        suggested_filename = f"{'s' * 300}.pdf"

        async def save_as(self, target: str) -> None:
            Path(target).write_bytes(b"%PDF-test")

    result = asyncio.run(
        rg.save_download(
            FakeDownload(),
            output_dir=tmp_path,
            target_prefix="p" * 300,
            max_pdf_bytes=100,
        )
    )

    output_path = Path(result["output_path"])
    assert result["ok"] is True
    assert len(output_path.name) <= 220
    assert output_path.read_bytes() == b"%PDF-test"


def test_parallel_downloads_for_same_item_use_distinct_paths(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    class FrozenDateTime:
        @classmethod
        def now(cls, _timezone: object) -> FrozenDateTime:
            return cls()

        def strftime(self, _format: str) -> str:
            return "20260719T191500Z"

    class FakeDownload:
        suggested_filename = "paper.pdf"

        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        async def save_as(self, target: str) -> None:
            await asyncio.sleep(0)
            Path(target).write_bytes(self.payload)

    monkeypatch.setattr(rg, "datetime", FrozenDateTime)

    async def save_both() -> list[dict[str, Any]]:
        first, second = await asyncio.gather(
            rg.save_download(
                FakeDownload(b"%PDF-first"),
                output_dir=tmp_path,
                target_prefix="ITEM1",
                max_pdf_bytes=100,
            ),
            rg.save_download(
                FakeDownload(b"%PDF-second"),
                output_dir=tmp_path,
                target_prefix="ITEM1",
                max_pdf_bytes=100,
            ),
        )
        return [first, second]

    results = asyncio.run(save_both())

    paths = [Path(result["output_path"]) for result in results]
    assert paths[0] != paths[1]
    assert {path.read_bytes() for path in paths} == {
        b"%PDF-first",
        b"%PDF-second",
    }


def test_save_download_removes_partial_output_on_cancellation(tmp_path: Path) -> None:
    written_paths: list[Path] = []

    class FakeDownload:
        suggested_filename = "paper.pdf"

        async def save_as(self, target: str) -> None:
            output_path = Path(target)
            written_paths.append(output_path)
            output_path.write_bytes(b"%PDF-partial")
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            rg.save_download(
                FakeDownload(),
                output_dir=tmp_path,
                target_prefix="ITEM1",
                max_pdf_bytes=100,
            )
        )

    assert len(written_paths) == 1
    assert not written_paths[0].exists()


def _run_args(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "url": "https://www.researchgate.net/publication/example",
        "item_key": "ITEM1",
        "data_dir": "",
        "output_dir": "",
        "profile_dir": "",
        "channel": "msedge",
        "headless": False,
        "timeout_seconds": 1,
        "manual_timeout_seconds": 0,
        "keep_open": False,
        "attach": False,
        "force_attach": False,
        "max_pdf_bytes": rg.DEFAULT_MAX_PDF_BYTES,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_run_rejects_malformed_preflight_skip_contract(monkeypatch: Any) -> None:
    async def unexpected_download(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("Malformed preflight must stop before browser download.")

    monkeypatch.setattr(rg, "from_env", lambda: SimpleNamespace())
    monkeypatch.setattr(
        rg,
        "preflight_pdf_attach",
        lambda *_args, **_kwargs: {"ok": True, "skipped": "false"},
    )
    monkeypatch.setattr(rg, "download_researchgate_pdf", unexpected_download)

    result = asyncio.run(rg.run(_run_args(attach=True)))

    assert result["ok"] is False
    assert result["status"] == "preflight_invalid_result"


def test_run_rejects_truthy_download_success(monkeypatch: Any) -> None:
    async def malformed_download(**_kwargs: Any) -> dict[str, Any]:
        return {
            "ok": "true",
            "status": "downloaded",
            "output_path": "paper.pdf",
        }

    monkeypatch.setattr(rg, "download_researchgate_pdf", malformed_download)

    result = asyncio.run(rg.run(_run_args()))

    assert result["ok"] is False
    assert result["status"] == "download_invalid_result"


def test_run_rejects_missing_download_output_path_before_attach(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(rg, "from_env", lambda: SimpleNamespace())
    monkeypatch.setattr(
        rg,
        "preflight_pdf_attach",
        lambda *_args, **_kwargs: {"ok": True, "skipped": False},
    )

    async def missing_output_path(**_kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "status": "downloaded"}

    monkeypatch.setattr(rg, "download_researchgate_pdf", missing_output_path)
    monkeypatch.setattr(
        rg,
        "attach_pdf_to_zotero_parent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Attach must not run without a validated output path.")
        ),
    )

    result = asyncio.run(rg.run(_run_args(attach=True)))

    assert result["ok"] is False
    assert result["status"] == "download_invalid_result"


def test_run_rejects_truthy_attach_success(monkeypatch: Any, tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-test")
    monkeypatch.setattr(rg, "from_env", lambda: SimpleNamespace())
    monkeypatch.setattr(
        rg,
        "preflight_pdf_attach",
        lambda *_args, **_kwargs: {"ok": True, "skipped": False},
    )

    async def successful_download(**_kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "status": "downloaded",
            "output_path": str(source),
        }

    monkeypatch.setattr(rg, "download_researchgate_pdf", successful_download)
    monkeypatch.setattr(
        rg,
        "attach_pdf_to_zotero_parent",
        lambda *_args, **_kwargs: {"ok": "true", "status": "attached"},
    )

    result = asyncio.run(rg.run(_run_args(attach=True)))

    assert result["ok"] is False
    assert result["status"] == "attach_invalid_result"


def test_main_requires_exact_boolean_success(monkeypatch: Any) -> None:
    async def malformed_run(_args: object) -> dict[str, Any]:
        return {"ok": "true"}

    monkeypatch.setattr(rg, "run", malformed_run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["researchgate_pdf_browser_download.py", "--url", "https://example.test/paper"],
    )

    assert rg.main() == 1


def test_save_download_removes_owned_output_when_validation_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeDownload:
        suggested_filename = "paper.pdf"

        async def save_as(self, target: str) -> None:
            Path(target).write_bytes(b"%PDF-test")

    original_open = Path.open
    interrupted = False

    def interrupt_validation(
        path: Path,
        mode: str = "r",
        *args: object,
        **kwargs: object,
    ) -> Any:
        nonlocal interrupted
        if (
            not interrupted
            and path.parent == tmp_path
            and path.suffix == ".pdf"
            and mode == "rb"
        ):
            interrupted = True
            raise KeyboardInterrupt
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", interrupt_validation)

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(
            rg.save_download(
                FakeDownload(),
                output_dir=tmp_path,
                target_prefix="ITEM1",
                max_pdf_bytes=100,
            )
        )

    assert interrupted is True
    assert list(tmp_path.glob("*.pdf")) == []


def test_run_preserves_exact_failed_preflight_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_download(**_kwargs: object) -> dict[str, object]:
        raise AssertionError("Failed preflight must stop before browser download.")

    monkeypatch.setattr(rg, "from_env", lambda: SimpleNamespace())
    monkeypatch.setattr(
        rg,
        "preflight_pdf_attach",
        lambda *_args, **_kwargs: {
            "ok": False,
            "skipped": False,
            "status": "item_not_found",
        },
    )
    monkeypatch.setattr(rg, "download_researchgate_pdf", unexpected_download)

    result = asyncio.run(rg.run(_run_args(attach=True)))

    assert result["ok"] is False
    assert result["status"] == "item_not_found"


def test_inventory_probe_attachment_key_rejects_numeric_key() -> None:
    assert rg.inventory_probe_attachment_key({"attachments": [{"key": 12345}]}) is None
