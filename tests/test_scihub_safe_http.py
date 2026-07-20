from __future__ import annotations

import socket
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from zotero_ingest_worker.metadata_processor_helpers import (
    _is_nonretryable_worker_error,
    _scihub_result_retryable,
)
import zotero_ingest_worker.scihub_pdf as scihub_pdf
from zotero_ingest_worker.scihub_pdf import (
    SciHubError,
    SciHubPdfOptions,
    SciHubResolveResult,
    SciHubTransportError,
    SciHubUnsafeUrlError,
    download_and_attach_scihub_pdf,
    download_scihub_pdf,
    resolve_pdf_url,
)
from zotero_metadata_enrichment import safe_http


class RedirectResponse:
    status = 302
    code = 302
    reason = "redirect"
    headers = {"Location": "http://127.0.0.1/private"}

    def close(self) -> None:
        return None


def test_scihub_private_redirect_is_blocked_before_second_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_resolve(url: str, **_kwargs: Any) -> tuple[object, ...]:
        if "127.0.0.1" in url:
            raise safe_http.UnsafeUrlError("blocked private redirect")
        return (object(),)

    def fake_open(request: object, **_kwargs: object) -> RedirectResponse:
        calls.append(request.full_url)  # type: ignore[attr-defined]
        return RedirectResponse()

    monkeypatch.setattr(safe_http, "_resolve_target", fake_resolve)
    monkeypatch.setattr(safe_http, "_open_pinned_once", fake_open)

    with pytest.raises(SciHubUnsafeUrlError):
        resolve_pdf_url(
            "10.1000/test",
            mirrors=("https://sci-hub.example/",),
        )

    assert calls == ["https://sci-hub.example/10.1000/test"]


def test_scihub_unsafe_result_is_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(
        "zotero_ingest_worker.scihub_pdf.resolve_pdf_url",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SciHubUnsafeUrlError("unsafe")),
    )

    result = download_scihub_pdf("10.1000/test", output_dir=tmp_path)

    assert result["status"] == "unsafe_url"
    assert _is_nonretryable_worker_error(safe_http.UnsafeUrlError("unsafe")) is True


def test_temporary_dns_error_remains_retryable_worker_error() -> None:
    error = socket.gaierror(socket.EAI_AGAIN, "temporary DNS failure")

    assert _is_nonretryable_worker_error(error) is False


def test_scihub_dns_failure_is_not_collapsed_into_terminal_no_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        safe_http,
        "_resolve_target",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            socket.gaierror(socket.EAI_AGAIN, "temporary DNS failure")
        ),
    )

    with pytest.raises(SciHubTransportError):
        resolve_pdf_url("10.1000/test", mirrors=("https://sci-hub.example/",))


def test_scihub_transport_result_remains_retryable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        "zotero_ingest_worker.scihub_pdf.resolve_pdf_url",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SciHubTransportError("temporary")
        ),
    )

    result = download_scihub_pdf("10.1000/test", output_dir=tmp_path)

    assert result["status"] == "transport_error"
    assert _scihub_result_retryable(result) is True


@pytest.mark.parametrize(
    ("status", "expected_error"),
    [(404, SciHubError), (503, SciHubTransportError)],
)
def test_scihub_http_status_preserves_terminal_vs_retryable_contract(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    expected_error: type[Exception],
) -> None:
    error = urllib.error.HTTPError(
        "https://sci-hub.example/10.1000/test",
        status,
        "test",
        {},
        None,
    )
    monkeypatch.setattr(
        "zotero_ingest_worker.scihub_pdf._resolve_on_mirror",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )

    with pytest.raises(expected_error) as caught:
        resolve_pdf_url("10.1000/test", mirrors=("https://sci-hub.example/",))

    if status == 404:
        assert not isinstance(caught.value, SciHubTransportError)


class PdfResponse:
    url = "https://files.example.test/article.pdf"
    headers = {"Content-Type": "application/pdf"}

    def __enter__(self) -> PdfResponse:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def read(self, _max_bytes: int) -> bytes:
        return b"%PDF-1.7\n"


def _install_scihub_pdf_download_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    identity: dict[str, object],
) -> None:
    monkeypatch.setattr(
        scihub_pdf,
        "resolve_pdf_url",
        lambda *_args, **_kwargs: SciHubResolveResult(
            doi="10.1000/test",
            scihub_url="https://sci-hub.example/10.1000/test",
            pdf_url="https://files.example.test/article.pdf",
        ),
    )
    monkeypatch.setattr(
        scihub_pdf,
        "package_validate_fetch_url",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, reason=""),
    )
    monkeypatch.setattr(
        scihub_pdf,
        "safe_urlopen",
        lambda *_args, **_kwargs: PdfResponse(),
    )
    monkeypatch.setattr(
        scihub_pdf,
        "package_assess_pdf_bytes_identity",
        lambda *_args, **_kwargs: dict(identity),
    )


@pytest.mark.parametrize(
    "invalid_needs_ocr",
    ["false", 1],
    ids=["string-false", "integer-one"],
)
def test_scihub_download_requires_exact_boolean_needs_ocr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    invalid_needs_ocr: object,
) -> None:
    _install_scihub_pdf_download_fakes(
        monkeypatch,
        identity={"ok": True, "needs_ocr": invalid_needs_ocr},
    )

    result = download_scihub_pdf("10.1000/test", output_dir=tmp_path)

    assert result["ok"] is True
    assert result["status"] == "downloaded"


def test_scihub_download_rejects_malformed_identity_ok(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _install_scihub_pdf_download_fakes(
        monkeypatch,
        identity={"ok": "true", "needs_ocr": False},
    )

    result = download_scihub_pdf(
        "10.1000/test",
        output_dir=tmp_path,
        expected_title="Expected article",
    )

    assert result["ok"] is False
    assert result["status"] == "identity_mismatch"
    assert list(tmp_path.glob("*.pdf")) == []


class FakeSciHubStore:
    def __init__(self, inventory: dict[str, object]) -> None:
        self.inventory = inventory

    def item_full_text_inventory(self, _metadata: object) -> dict[str, object]:
        return dict(self.inventory)


def _fake_scihub_module(
    *,
    inventory: dict[str, object],
    attach_result: dict[str, object] | None = None,
    attach_calls: list[Path] | None = None,
) -> SimpleNamespace:
    metadata = SimpleNamespace(title="Article", fields={})
    store = FakeSciHubStore(inventory)

    def attach_pdf_to_zotero_parent(
        _config: object,
        *,
        source_path: Path,
        **_kwargs: object,
    ) -> dict[str, object]:
        if attach_calls is not None:
            attach_calls.append(source_path)
        return dict(attach_result or {"ok": True})

    return SimpleNamespace(
        find_item=lambda *_args, **_kwargs: (metadata, store),
        attach_pdf_to_zotero_parent=attach_pdf_to_zotero_parent,
    )


def test_scihub_wrapper_requires_exact_boolean_inventory_has_pdf(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    download_calls: list[str] = []
    monkeypatch.setattr(
        scihub_pdf,
        "_script_module",
        lambda: _fake_scihub_module(inventory={"has_pdf": "false"}),
    )
    monkeypatch.setattr(
        scihub_pdf,
        "download_scihub_pdf",
        lambda doi, **_kwargs: (
            download_calls.append(doi) or {"ok": False, "status": "unresolved"}
        ),
    )

    result = download_and_attach_scihub_pdf(
        SimpleNamespace(),
        SciHubPdfOptions(
            item_key="ITEM1",
            doi="10.1000/test",
            output_dir=tmp_path,
        ),
    )

    assert download_calls == ["10.1000/test"]
    assert result["ok"] is False
    assert result["status"] == "unresolved"


@pytest.mark.parametrize(
    ("stage", "expected_status"),
    [("download", "download_invalid_result"), ("attach", "attach_invalid_result")],
)
def test_scihub_wrapper_rejects_malformed_success_results(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    stage: str,
    expected_status: str,
) -> None:
    attach_calls: list[Path] = []
    attach_result = {"ok": "true", "status": "attached"}
    monkeypatch.setattr(
        scihub_pdf,
        "_script_module",
        lambda: _fake_scihub_module(
            inventory={"has_pdf": False},
            attach_result=attach_result,
            attach_calls=attach_calls,
        ),
    )
    download_ok: object = "true" if stage == "download" else True
    monkeypatch.setattr(
        scihub_pdf,
        "download_scihub_pdf",
        lambda *_args, **_kwargs: {
            "ok": download_ok,
            "status": "downloaded",
            "output_path": str(tmp_path / "article.pdf"),
        },
    )

    result = download_and_attach_scihub_pdf(
        SimpleNamespace(),
        SciHubPdfOptions(item_key="ITEM1", doi="10.1000/test"),
    )

    assert result["ok"] is False
    assert result["status"] == expected_status
    assert len(attach_calls) == (0 if stage == "download" else 1)


def test_scihub_lease_guard_blocks_attach_after_download(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    attach_calls: list[Path] = []
    monkeypatch.setattr(
        scihub_pdf,
        "_script_module",
        lambda: _fake_scihub_module(
            inventory={"has_pdf": False},
            attach_calls=attach_calls,
        ),
    )
    monkeypatch.setattr(
        scihub_pdf,
        "download_scihub_pdf",
        lambda *_args, **_kwargs: {
            "ok": True,
            "status": "downloaded",
            "output_path": str(tmp_path / "article.pdf"),
        },
    )

    with pytest.raises(RuntimeError, match="metadata lease lost"):
        download_and_attach_scihub_pdf(
            SimpleNamespace(),
            SciHubPdfOptions(
                item_key="ITEM1",
                doi="10.1000/test",
                ensure_active=lambda: (_ for _ in ()).throw(
                    RuntimeError("metadata lease lost")
                ),
            ),
        )

    assert attach_calls == []


class PayloadPdfResponse(PdfResponse):
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self, _max_bytes: int) -> bytes:
        return self.payload


def test_scihub_parallel_downloads_use_distinct_owned_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FrozenDateTime:
        @classmethod
        def now(cls, _timezone: object) -> FrozenDateTime:
            return cls()

        def strftime(self, _format: str) -> str:
            return "20260719T232100Z"

    _install_scihub_pdf_download_fakes(
        monkeypatch,
        identity={"ok": True, "needs_ocr": False},
    )
    payloads = iter((b"%PDF-first", b"%PDF-second"))
    monkeypatch.setattr(scihub_pdf, "datetime", FrozenDateTime)
    monkeypatch.setattr(
        scihub_pdf,
        "safe_urlopen",
        lambda *_args, **_kwargs: PayloadPdfResponse(next(payloads)),
    )

    first = download_scihub_pdf("10.1000/test", output_dir=tmp_path)
    second = download_scihub_pdf("10.1000/test", output_dir=tmp_path)

    first_path = Path(first["output_path"])
    second_path = Path(second["output_path"])
    assert first_path != second_path
    assert first_path.read_bytes() == b"%PDF-first"
    assert second_path.read_bytes() == b"%PDF-second"


def test_scihub_download_removes_owned_output_when_identity_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_scihub_pdf_download_fakes(
        monkeypatch,
        identity={"ok": True, "needs_ocr": False},
    )
    monkeypatch.setattr(
        scihub_pdf,
        "package_assess_pdf_bytes_identity",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        download_scihub_pdf("10.1000/test", output_dir=tmp_path)

    assert list(tmp_path.glob("*.pdf")) == []


@pytest.mark.parametrize(
    ("http_status", "expected_retryable"),
    [(404, False), (503, True)],
)
def test_scihub_download_http_status_preserves_retry_taxonomy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    http_status: int,
    expected_retryable: bool,
) -> None:
    monkeypatch.setattr(
        scihub_pdf,
        "resolve_pdf_url",
        lambda *_args, **_kwargs: SciHubResolveResult(
            doi="10.1000/test",
            scihub_url="https://sci-hub.example/10.1000/test",
            pdf_url="https://files.example.test/article.pdf",
        ),
    )
    monkeypatch.setattr(
        scihub_pdf,
        "package_validate_fetch_url",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, reason=""),
    )
    error = urllib.error.HTTPError(
        "https://files.example.test/article.pdf",
        http_status,
        "test",
        {},
        None,
    )
    monkeypatch.setattr(
        scihub_pdf,
        "safe_urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )

    result = download_scihub_pdf("10.1000/test", output_dir=tmp_path)

    assert result["status"] == "http_error"
    assert result["http_status"] == http_status
    assert _scihub_result_retryable(result) is expected_retryable


@pytest.mark.parametrize(
    ("stage", "expected_status"),
    [
        ("download_nonmapping", "download_invalid_result"),
        ("download_missing_path", "download_invalid_result"),
        ("attach_nonmapping", "attach_invalid_result"),
    ],
)
def test_scihub_wrapper_rejects_nonmapping_and_incomplete_adapter_results(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stage: str,
    expected_status: str,
) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-test")
    attach_calls: list[Path] = []
    metadata = SimpleNamespace(title="Article", fields={})
    store = SimpleNamespace(
        item_full_text_inventory=lambda _metadata: {"has_pdf": False}
    )

    def attach(
        _config: object,
        *,
        source_path: Path,
        **_kwargs: object,
    ) -> object:
        attach_calls.append(source_path)
        if stage == "attach_nonmapping":
            return ["malformed"]
        return {"ok": True, "status": "attached"}

    monkeypatch.setattr(
        scihub_pdf,
        "_script_module",
        lambda: SimpleNamespace(
            find_item=lambda *_args, **_kwargs: (metadata, store),
            attach_pdf_to_zotero_parent=attach,
        ),
    )
    if stage == "download_nonmapping":
        download_result: object = ["malformed"]
    elif stage == "download_missing_path":
        download_result = {"ok": True, "status": "downloaded"}
    else:
        download_result = {
            "ok": True,
            "status": "downloaded",
            "output_path": str(source),
        }
    monkeypatch.setattr(
        scihub_pdf,
        "download_scihub_pdf",
        lambda *_args, **_kwargs: download_result,
    )

    result = download_and_attach_scihub_pdf(
        SimpleNamespace(),
        SciHubPdfOptions(item_key="ITEM1", doi="10.1000/test"),
    )

    assert result["ok"] is False
    assert result["status"] == expected_status
    assert len(attach_calls) == (1 if stage == "attach_nonmapping" else 0)


def test_scihub_rejects_html_even_when_server_claims_pdf_mime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_scihub_pdf_download_fakes(
        monkeypatch,
        identity={"ok": True, "needs_ocr": False},
    )

    class FalsePdfResponse(PdfResponse):
        headers = {"Content-Type": "application/pdf"}

        def read(self, _max_bytes: int) -> bytes:
            return b"<html>login required</html>"

    monkeypatch.setattr(
        scihub_pdf,
        "safe_urlopen",
        lambda *_args, **_kwargs: FalsePdfResponse(),
    )

    result = download_scihub_pdf("10.1000/test", output_dir=tmp_path)

    assert result["ok"] is False
    assert result["status"] == "non_pdf"
    assert list(tmp_path.glob("*.pdf")) == []


def test_scihub_full_url_query_stays_under_mirror_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []

    class HtmlResponse:
        headers: dict[str, str] = {}

        def __init__(self, url: str) -> None:
            self.url = url

        def __enter__(self) -> HtmlResponse:
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

        def read(self, _max_bytes: int) -> bytes:
            return b'<embed id="pdf" src="/storage/article.pdf">'

    def open_url(request: object, **_kwargs: object) -> HtmlResponse:
        url = str(request.full_url)  # type: ignore[attr-defined]
        opened.append(url)
        return HtmlResponse(url)

    monkeypatch.setattr(
        scihub_pdf,
        "package_validate_fetch_url",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, reason=""),
    )
    monkeypatch.setattr(scihub_pdf, "safe_urlopen", open_url)

    result = scihub_pdf._resolve_on_mirror(
        "https://publisher.example/article",
        base_url="https://sci-hub.example/",
        user_agent="test",
        timeout_seconds=1,
        max_bytes=1000,
    )

    assert opened[0].startswith("https://sci-hub.example/")
    assert "publisher.example/article" in opened[0]
    assert result.scihub_url == opened[0]


def test_scihub_negative_size_limit_is_clamped_before_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_scihub_pdf_download_fakes(
        monkeypatch,
        identity={"ok": True, "needs_ocr": False},
    )
    read_limits: list[int] = []

    class RecordingResponse(PdfResponse):
        def read(self, max_bytes: int) -> bytes:
            read_limits.append(max_bytes)
            return b"%PDF-test"

    monkeypatch.setattr(
        scihub_pdf,
        "safe_urlopen",
        lambda *_args, **_kwargs: RecordingResponse(),
    )

    result = download_scihub_pdf(
        "10.1000/test",
        output_dir=tmp_path,
        max_bytes=-5,
    )

    assert read_limits == [2]
    assert result["ok"] is False
    assert result["status"] == "too_large"
