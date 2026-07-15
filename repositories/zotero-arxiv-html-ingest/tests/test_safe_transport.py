from __future__ import annotations

import socket
import urllib.request
from typing import Any

import pytest
from zotero_metadata_enrichment import provider_http, safe_http

from zotero_arxiv_html_ingest.html_fetch import ArxivHtmlClient
from zotero_arxiv_html_ingest.lookup import ArxivLookupClient


class FakeResponse:
    status = 302
    code = 302
    reason = "test"
    headers = {"Location": "https://attacker.example/private"}

    def close(self) -> None:
        pass


def _public_dns(_host: str, port: int, **_kwargs: Any) -> list[tuple[Any, ...]]:
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("93.184.216.34", port),
        )
    ]


@pytest.mark.parametrize(
    "operation",
    [
        lambda: ArxivHtmlClient().fetch("2401.01234"),
        lambda: ArxivLookupClient().by_id("2401.01234"),
    ],
)
def test_arxiv_clients_reject_cross_host_redirect_before_second_open(
    monkeypatch: pytest.MonkeyPatch,
    operation: Any,
) -> None:
    opened: list[str] = []
    provider_http._GLOBAL_HOST_THROTTLE.reset()
    monkeypatch.setattr(safe_http.socket, "getaddrinfo", _public_dns)

    def fake_open(request: urllib.request.Request, **_kwargs: Any) -> FakeResponse:
        opened.append(request.full_url)
        return FakeResponse()

    monkeypatch.setattr(safe_http, "_open_pinned_once", fake_open)

    with pytest.raises(safe_http.UnsafeUrlError, match="Redirect policy rejected"):
        operation()

    assert len(opened) == 1
    assert opened[0].startswith("https://arxiv.org/")
