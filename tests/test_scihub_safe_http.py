from __future__ import annotations

import socket
import urllib.error
from typing import Any

import pytest

from zotero_ingest_worker.metadata_processor_helpers import (
    _is_nonretryable_worker_error,
    _scihub_result_retryable,
)
from zotero_ingest_worker.scihub_pdf import (
    SciHubError,
    SciHubTransportError,
    SciHubUnsafeUrlError,
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


def test_scihub_unsafe_result_is_terminal(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
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
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SciHubTransportError("temporary")),
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
