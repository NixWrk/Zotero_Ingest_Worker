from __future__ import annotations

import asyncio
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import zotero_ingest_worker.metadata_processor as metadata_processor
import zotero_ingest_worker.metadata_processor_helpers as metadata_helpers_module
import zotero_ingest_worker.researchgate_pdf as researchgate_pdf
from zotero_ingest_worker.metadata_processor import ZoteroMetadataProcessor
from zotero_ingest_worker.local_zotero import LocalItemMetadata


def test_researchgate_pdf_adapter_uses_passed_config(
    monkeypatch: Any, tmp_path: Path
) -> None:
    calls: dict[str, Any] = {}
    config = SimpleNamespace(name="config")

    async def fake_download(**kwargs: Any) -> dict[str, Any]:
        calls["download"] = kwargs
        output_path = tmp_path / "paper.pdf"
        output_path.write_bytes(b"%PDF")
        return {"ok": True, "status": "downloaded", "output_path": str(output_path)}

    def fake_attach(passed_config: object, **kwargs: Any) -> dict[str, Any]:
        calls["attach_config"] = passed_config
        calls["attach"] = kwargs
        return {
            "ok": True,
            "status": "attached",
            "relay": {"newAttachmentKey": "PDF1234"},
        }

    monkeypatch.setattr(
        researchgate_pdf,
        "_script_module",
        lambda: SimpleNamespace(
            preflight_pdf_attach=lambda *_args, **_kwargs: {
                "ok": True,
                "skipped": False,
            },
            download_researchgate_pdf=fake_download,
            attach_pdf_to_zotero_parent=fake_attach,
            DEFAULT_DOWNLOAD_DIR=tmp_path,
            DEFAULT_PROFILE_DIR=tmp_path / "profile",
        ),
    )

    result = asyncio.run(
        researchgate_pdf.download_and_attach_researchgate_pdf(
            config,  # type: ignore[arg-type]
            researchgate_pdf.ResearchGatePdfOptions(
                url="https://www.researchgate.net/publication/example",
                item_key="ITEM1",
                data_dir="Zotero",
            ),
        )
    )

    assert result["ok"] is True
    assert (
        calls["download"]["url"] == "https://www.researchgate.net/publication/example"
    )
    assert calls["attach_config"] is config
    assert calls["attach"]["item_key"] == "ITEM1"


@pytest.mark.parametrize(
    ("stage", "expected_status"),
    [
        ("preflight-ok", "preflight_invalid_result"),
        ("preflight-skipped", "preflight_invalid_result"),
        ("download", "download_invalid_result"),
        ("attach", "attach_invalid_result"),
    ],
)
def test_researchgate_adapter_rejects_malformed_success_contracts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stage: str,
    expected_status: str,
) -> None:
    download_calls: list[str] = []
    attach_calls: list[Path] = []

    def preflight(*_args: object, **_kwargs: object) -> dict[str, object]:
        if stage == "preflight-ok":
            return {"ok": "true", "skipped": False}
        if stage == "preflight-skipped":
            return {"ok": True, "skipped": "false"}
        return {"ok": True, "skipped": False}

    async def download(**_kwargs: object) -> dict[str, object]:
        download_calls.append("download")
        output_path = tmp_path / "paper.pdf"
        output_path.write_bytes(b"%PDF")
        return {
            "ok": "true" if stage == "download" else True,
            "status": "downloaded",
            "output_path": str(output_path),
        }

    def attach(
        _config: object,
        *,
        source_path: Path,
        **_kwargs: object,
    ) -> dict[str, object]:
        attach_calls.append(source_path)
        return {
            "ok": "true" if stage == "attach" else True,
            "status": "attached",
        }

    monkeypatch.setattr(
        researchgate_pdf,
        "_script_module",
        lambda: SimpleNamespace(
            preflight_pdf_attach=preflight,
            download_researchgate_pdf=download,
            attach_pdf_to_zotero_parent=attach,
            DEFAULT_DOWNLOAD_DIR=tmp_path,
            DEFAULT_PROFILE_DIR=tmp_path / "profile",
        ),
    )

    result = asyncio.run(
        researchgate_pdf.download_and_attach_researchgate_pdf(
            SimpleNamespace(),  # type: ignore[arg-type]
            researchgate_pdf.ResearchGatePdfOptions(
                url="https://www.researchgate.net/publication/example",
                item_key="ITEM1",
            ),
        )
    )

    assert result["ok"] is False
    assert result["status"] == expected_status
    assert len(download_calls) == (0 if stage.startswith("preflight") else 1)
    assert len(attach_calls) == (1 if stage == "attach" else 0)


def test_researchgate_pdf_drain_job_marks_success(
    monkeypatch: Any, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    async def fake_download_and_attach(
        _config: object, options: object
    ) -> dict[str, Any]:
        captured["options"] = options
        output_path = tmp_path / "paper.pdf"
        output_path.write_bytes(b"%PDF")
        return {
            "ok": True,
            "status": "attached",
            "download": {"ok": True, "output_path": str(output_path)},
            "attach": {"ok": True, "relay": {"newAttachmentKey": "PDF1234"}},
        }

    class FakeState:
        def mark_metadata_job_succeeded(self, **kwargs: Any) -> dict[str, Any]:
            captured["succeeded"] = kwargs
            return {"status": "succeeded", **kwargs}

        def mark_metadata_job_skipped(self, **kwargs: Any) -> dict[str, Any]:
            captured["skipped"] = kwargs
            return {"status": "skipped", **kwargs}

        def mark_metadata_job_failed(self, **kwargs: Any) -> dict[str, Any]:
            captured["failed"] = kwargs
            return {"status": "failed_final", **kwargs}

    monkeypatch.setattr(
        metadata_processor,
        "download_and_attach_researchgate_pdf",
        fake_download_and_attach,
    )
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace()
    processor.state = FakeState()
    url = "https://www.researchgate.net/publication/example"

    result = processor._drain_researchgate_pdf_job(
        {
            "job_id": "job_1",
            "lease_owner": "owner-a",
            "parent_item_key": "ITEM1",
            "attachment_key": "ITEM1",
            "data_dir": str(tmp_path),
            "queue_key": "v=researchgate-pdf-browser-1|url=https%3A%2F%2Fwww.researchgate.net%2Fpublication%2Fexample",
        }
    )

    assert result["status"] == "succeeded"
    assert captured["options"].url == url
    assert captured["options"].item_key == "ITEM1"
    assert captured["succeeded"]["output_path"] == str(tmp_path / "paper.pdf")
    assert captured["succeeded"]["owner"] == "owner-a"


def test_researchgate_pdf_drain_rejects_malformed_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_download_and_attach(
        _config: object,
        _options: object,
    ) -> dict[str, Any]:
        return {
            "ok": "true",
            "status": "attached",
            "download": {
                "ok": True,
                "output_path": str(tmp_path / "paper.pdf"),
            },
        }

    class FakeState:
        def mark_metadata_job_succeeded(self, **kwargs: Any) -> dict[str, Any]:
            captured["succeeded"] = kwargs
            return {"status": "succeeded", **kwargs}

        def mark_metadata_job_skipped(self, **kwargs: Any) -> dict[str, Any]:
            captured["skipped"] = kwargs
            return {"status": "skipped", **kwargs}

        def mark_metadata_job_failed(self, **kwargs: Any) -> dict[str, Any]:
            captured["failed"] = kwargs
            return {"status": "failed_retryable", **kwargs}

    monkeypatch.setattr(
        metadata_processor,
        "download_and_attach_researchgate_pdf",
        fake_download_and_attach,
    )
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace()
    processor.state = FakeState()

    result = processor._drain_researchgate_pdf_job(
        {
            "job_id": "job_1",
            "lease_owner": "owner-a",
            "parent_item_key": "ITEM1",
            "data_dir": str(tmp_path),
            "queue_key": (
                "v=researchgate-pdf-browser-1|url=https%3A%2F%2F"
                "www.researchgate.net%2Fpublication%2Fexample"
            ),
        }
    )

    assert result["status"] == "failed_retryable"
    assert "succeeded" not in captured
    assert captured["failed"]["result"]["status"] == "invalid_result"
    assert captured["failed"]["result"]["upstream_status"] == "attached"


def test_successful_pdf_selector_requires_exact_boolean() -> None:
    assert (
        metadata_processor._first_successful_pdf_download(
            [{"ok": "true", "output_path": "paper.pdf"}]
        )
        is None
    )


@pytest.mark.parametrize("output_path", [True, 1, {}, []])
def test_successful_pdf_selector_requires_exact_nonempty_string(
    output_path: object,
) -> None:
    assert (
        metadata_processor._first_successful_pdf_download(
            [{"ok": True, "output_path": output_path}]
        )
        is None
    )


def test_successful_pdf_selector_accepts_nonempty_string() -> None:
    item = {"ok": True, "output_path": " paper.pdf "}

    assert metadata_processor._first_successful_pdf_download([item]) is item


@pytest.mark.parametrize(
    ("url", "accepted"),
    [
        ("https://www.researchgate.net/publication/example", True),
        ("http://www.researchgate.net/publication/example", False),
        ("https://user:secret@researchgate.net/publication/example", False),
        ("https://researchgate.net:444/publication/example", False),
        ("https://researchgate.net.evil.test/publication/example", False),
        (" https://www.researchgate.net/publication/example", False),
    ],
)
def test_researchgate_job_url_decoder_revalidates_canonical_url(
    url: str,
    accepted: bool,
) -> None:
    encoded = urllib.parse.quote(url, safe="")

    decoded = metadata_processor._researchgate_url_from_job(
        {"queue_key": f"v=researchgate-pdf-browser-1|url={encoded}"}
    )

    assert decoded == (url if accepted else "")


def test_researchgate_job_url_decoder_stops_at_queue_field_delimiter() -> None:
    first = urllib.parse.quote(
        "https://www.researchgate.net/publication/first", safe=""
    )
    second = urllib.parse.quote("https://example.test/second", safe="")

    assert (
        metadata_processor._researchgate_url_from_job(
            {
                "queue_key": (
                    f"v=researchgate-pdf-browser-1|url={first}|next_url={second}"
                )
            }
        )
        == "https://www.researchgate.net/publication/first"
    )


def test_researchgate_job_url_decoder_checks_utf8_budget_before_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded = "é" * (
        metadata_helpers_module.MAX_RESEARCHGATE_ENCODED_URL_BYTES // 2 + 1
    )

    def fail_unquote(_value: str) -> str:
        raise AssertionError("oversized payload must be rejected before decoding")

    monkeypatch.setattr(metadata_helpers_module.urllib.parse, "unquote", fail_unquote)

    assert (
        metadata_processor._researchgate_url_from_job(
            {"queue_key": f"v=researchgate-pdf-browser-1|url={encoded}"}
        )
        == ""
    )


def test_researchgate_job_url_decoder_rejects_non_string_queue_key() -> None:
    encoded = urllib.parse.quote(
        "https://www.researchgate.net/publication/example", safe=""
    )

    assert (
        metadata_processor._researchgate_url_from_job(
            {"queue_key": [f"v=researchgate-pdf-browser-1|url={encoded}"]}
        )
        == ""
    )


def test_researchgate_job_url_decoder_rejects_oversized_decoded_url() -> None:
    url = "https://www.researchgate.net/publication/" + (
        "x" * metadata_helpers_module.MAX_RESEARCHGATE_URL_CHARS
    )
    encoded = urllib.parse.quote(url, safe="")

    assert (
        metadata_processor._researchgate_url_from_job(
            {"queue_key": f"v=researchgate-pdf-browser-1|url={encoded}"}
        )
        == ""
    )


def test_researchgate_fallback_ignores_non_researchgate_urls() -> None:
    payload = {
        "browser_fallbacks": [
            {"url": "https://example.test/browser-only"},
            {"url": "https://www.researchgate.net/publication/example"},
        ]
    }

    fallback = metadata_processor._first_researchgate_browser_fallback(payload)

    assert fallback == {"url": "https://www.researchgate.net/publication/example"}


def test_researchgate_browser_fallback_enqueues_without_undefined_force(
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    class FakeState:
        def enqueue_metadata_job(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"job_id": "researchgate-1", "created": True, **kwargs}

    sqlite_path = tmp_path / "zotero.sqlite"
    sqlite_path.write_bytes(b"state")
    processor = ZoteroMetadataProcessor.__new__(ZoteroMetadataProcessor)
    processor.config = SimpleNamespace()
    processor.state = FakeState()
    processor._queue_key = lambda _job_type: "researchgate-v1"
    metadata = LocalItemMetadata(
        library_id="LIB1",
        data_dir=tmp_path,
        key="PARENT1",
        item_id=1,
        version=None,
        item_type="journalArticle",
        date_modified=None,
        fields={"title": "Paper"},
        creators=[],
        tags=[],
        collections=[],
        relations=[],
    )

    result = processor._enqueue_researchgate_pdf_fallback(
        metadata=metadata,
        payload={
            "browser_fallbacks": [
                {"url": "https://www.researchgate.net/publication/example"}
            ]
        },
        reason="test",
    )

    assert result is not None
    assert result["classification"] == "queued"
    assert captured["force"] is False
    assert captured["parent_item_key"] == "PARENT1"


def test_researchgate_lease_guard_blocks_attach_after_download(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    attach_calls: list[Path] = []

    async def download(**_kwargs: object) -> dict[str, object]:
        output_path = tmp_path / "paper.pdf"
        output_path.write_bytes(b"%PDF")
        return {"ok": True, "status": "downloaded", "output_path": str(output_path)}

    def attach(
        _config: object,
        *,
        source_path: Path,
        **_kwargs: object,
    ) -> dict[str, object]:
        attach_calls.append(source_path)
        return {"ok": True, "status": "attached"}

    monkeypatch.setattr(
        researchgate_pdf,
        "_script_module",
        lambda: SimpleNamespace(
            preflight_pdf_attach=lambda *_args, **_kwargs: {
                "ok": True,
                "skipped": False,
            },
            download_researchgate_pdf=download,
            attach_pdf_to_zotero_parent=attach,
            DEFAULT_DOWNLOAD_DIR=tmp_path,
            DEFAULT_PROFILE_DIR=tmp_path / "profile",
        ),
    )

    with pytest.raises(RuntimeError, match="metadata lease lost"):
        asyncio.run(
            researchgate_pdf.download_and_attach_researchgate_pdf(
                SimpleNamespace(),  # type: ignore[arg-type]
                researchgate_pdf.ResearchGatePdfOptions(
                    url="https://www.researchgate.net/publication/example",
                    item_key="ITEM1",
                    ensure_active=lambda: (_ for _ in ()).throw(
                        RuntimeError("metadata lease lost")
                    ),
                ),
            )
        )

    assert attach_calls == []


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("unsafe_browser_url", False),
        ("network_policy_blocked", False),
        ("preflight_invalid_result", False),
        ("download_invalid_result", False),
        ("attach_invalid_result", False),
        ("download_not_pdf", False),
        ("item_not_found", False),
        ("browser_error", True),
        ("download_not_triggered", True),
        ("attachment_snapshot_failed", True),
        ("relay_attachment_invalid_result", True),
    ],
)
def test_researchgate_retry_taxonomy_is_explicit(
    status: str,
    expected: bool,
) -> None:
    assert (
        metadata_processor._researchgate_result_retryable(
            {"ok": False, "status": status}
        )
        is expected
    )


@pytest.mark.parametrize(
    ("url", "accepted"),
    [
        ("https://www.researchgate.net/publication/example", True),
        ("http://www.researchgate.net/publication/example", False),
        ("javascript://researchgate.net/publication/example", False),
        ("https://user:secret@researchgate.net/publication/example", False),
        ("https://researchgate.net:444/publication/example", False),
    ],
)
def test_researchgate_fallback_requires_canonical_https_url(
    url: str,
    accepted: bool,
) -> None:
    fallback = metadata_processor._first_researchgate_browser_fallback(
        {"browser_fallbacks": [{"url": url}]}
    )

    assert (fallback is not None) is accepted


def test_researchgate_adapter_classifies_missing_parent_as_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def unexpected_download(**_kwargs: object) -> dict[str, object]:
        raise AssertionError("Missing parent must stop before browser download.")

    monkeypatch.setattr(
        researchgate_pdf,
        "_script_module",
        lambda: SimpleNamespace(
            preflight_pdf_attach=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                FileNotFoundError("missing parent")
            ),
            download_researchgate_pdf=unexpected_download,
            attach_pdf_to_zotero_parent=lambda *_args, **_kwargs: {"ok": True},
            DEFAULT_DOWNLOAD_DIR=tmp_path,
            DEFAULT_PROFILE_DIR=tmp_path / "profile",
        ),
    )

    result = asyncio.run(
        researchgate_pdf.download_and_attach_researchgate_pdf(
            SimpleNamespace(),  # type: ignore[arg-type]
            researchgate_pdf.ResearchGatePdfOptions(
                url="https://www.researchgate.net/publication/example",
                item_key="MISSING",
            ),
        )
    )

    assert result["ok"] is False
    assert result["status"] == "item_not_found"
