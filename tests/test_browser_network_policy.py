from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from zotero_ingest_worker.browser_network_policy import (
    BrowserNetworkAudit,
    evaluate_browser_request,
    install_browser_network_policy,
    validate_researchgate_initial_url,
)


def _public(_url: str) -> list[SimpleNamespace]:
    return [SimpleNamespace(ip="93.184.216.34")]


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://www.researchgate.net/publication/1", "https_required"),
        ("https://example.org/publication/1", "initial_host_not_researchgate"),
        ("https://user:secret@www.researchgate.net/publication/1", "credentials_forbidden"),
        ("https://www.researchgate.net:8443/publication/1", "nonstandard_port"),
    ],
)
def test_initial_researchgate_url_rejects_unsafe_shape_before_dns(
    url: str,
    reason: str,
) -> None:
    calls: list[str] = []

    decision = validate_researchgate_initial_url(
        url,
        resolve_target=lambda value: calls.append(value) or _public(value),
    )

    assert decision.allowed is False
    assert decision.reason == reason
    assert calls == []
    assert "secret" not in decision.to_audit_dict()["url"]


def test_initial_researchgate_url_resolves_all_addresses_before_allowing() -> None:
    decision = validate_researchgate_initial_url(
        "https://www.researchgate.net/publication/1",
        resolve_target=lambda _url: [
            SimpleNamespace(ip="93.184.216.34"),
            SimpleNamespace(ip="10.0.0.7"),
        ],
    )

    assert decision.allowed is False
    assert decision.reason == "blocked_resolved_address"
    assert decision.resolved_addresses == ("93.184.216.34", "10.0.0.7")


def test_researchgate_navigation_and_explicit_external_pdf_are_allowed() -> None:
    researchgate = evaluate_browser_request(
        "https://www.researchgate.net/publication/1",
        resource_type="document",
        navigation=True,
        resolve_target=_public,
    )
    external_pdf = evaluate_browser_request(
        "https://download.example.org/files/paper.pdf?token=secret",
        resource_type="document",
        navigation=True,
        redirected_from_url="https://www.researchgate.net/publication/1",
        resolve_target=_public,
    )

    assert researchgate.allowed is True
    assert researchgate.category == "researchgate_navigation"
    assert external_pdf.allowed is True
    assert external_pdf.category == "external_pdf_download"
    assert "token=secret" not in external_pdf.to_audit_dict()["url"]


def test_cross_host_html_navigation_and_private_pdf_redirect_are_blocked() -> None:
    cross_host = evaluate_browser_request(
        "https://example.org/login",
        resource_type="document",
        navigation=True,
        redirected_from_url="https://www.researchgate.net/publication/1",
        resolve_target=_public,
    )
    private_pdf = evaluate_browser_request(
        "https://download.example.org/paper.pdf",
        resource_type="document",
        navigation=True,
        redirected_from_url="https://www.researchgate.net/publication/1",
        resolve_target=lambda _url: [SimpleNamespace(ip="127.0.0.1")],
    )

    assert cross_host.allowed is False
    assert cross_host.reason == "cross_host_navigation_blocked"
    assert private_pdf.allowed is False
    assert private_pdf.reason == "blocked_resolved_address"


def test_public_https_subresource_is_allowed_but_http_is_blocked() -> None:
    public = evaluate_browser_request(
        "https://fonts.example.org/font.woff2",
        resource_type="font",
        navigation=False,
        initiator_url="https://www.researchgate.net/publication/1",
        resolve_target=_public,
    )
    downgrade = evaluate_browser_request(
        "http://fonts.example.org/font.woff2",
        resource_type="font",
        navigation=False,
        initiator_url="https://www.researchgate.net/publication/1",
        resolve_target=_public,
    )

    assert public.allowed is True
    assert public.category == "external_public_subresource"
    assert downgrade.allowed is False
    assert downgrade.reason == "https_required"


def test_blob_resource_requires_researchgate_origin_and_initiator() -> None:
    allowed = evaluate_browser_request(
        "blob:https://www.researchgate.net/1234",
        resource_type="fetch",
        navigation=False,
        initiator_url="https://www.researchgate.net/publication/1",
    )
    blocked = evaluate_browser_request(
        "blob:https://example.org/1234",
        resource_type="fetch",
        navigation=False,
        initiator_url="https://www.researchgate.net/publication/1",
    )

    assert allowed.allowed is True
    assert blocked.allowed is False
    assert blocked.reason == "untrusted_blob_origin"


def test_installed_route_continues_public_and_aborts_private() -> None:
    class FakeContext:
        handler: Any = None

        async def route(self, pattern: str, handler: Any) -> None:
            assert pattern == "**/*"
            self.handler = handler

    class FakeRoute:
        continued = False
        aborted = False

        async def continue_(self) -> None:
            self.continued = True

        async def abort(self, *, error_code: str) -> None:
            assert error_code == "blockedbyclient"
            self.aborted = True

    async def exercise() -> tuple[FakeRoute, FakeRoute, BrowserNetworkAudit]:
        context = FakeContext()
        audit = BrowserNetworkAudit()

        def resolver(url: str) -> list[SimpleNamespace]:
            address = "127.0.0.1" if "private.example" in url else "93.184.216.34"
            return [SimpleNamespace(ip=address)]

        await install_browser_network_policy(context, audit, resolve_target=resolver)
        allowed_route = FakeRoute()
        blocked_route = FakeRoute()
        await context.handler(
            allowed_route,
            SimpleNamespace(
                url="https://www.researchgate.net/publication/1",
                resource_type="document",
                is_navigation_request=lambda: True,
                redirected_from=None,
                frame=SimpleNamespace(url="about:blank"),
            ),
        )
        await context.handler(
            blocked_route,
            SimpleNamespace(
                url="https://private.example/paper.pdf",
                resource_type="document",
                is_navigation_request=lambda: True,
                redirected_from=SimpleNamespace(
                    url="https://www.researchgate.net/publication/1"
                ),
                frame=SimpleNamespace(url="https://www.researchgate.net/publication/1"),
            ),
        )
        return allowed_route, blocked_route, audit

    allowed_route, blocked_route, audit = asyncio.run(exercise())

    assert allowed_route.continued is True
    assert allowed_route.aborted is False
    assert blocked_route.continued is False
    assert blocked_route.aborted is True
    assert audit.allowed_navigation == 1
    assert audit.blocked_navigation == 1


def test_route_policy_errors_fail_closed() -> None:
    class FakeContext:
        handler: Any = None

        async def route(self, _pattern: str, handler: Any) -> None:
            self.handler = handler

    class FakeRoute:
        aborted = False

        async def continue_(self) -> None:
            raise AssertionError("policy failure must never continue the request")

        async def abort(self, *, error_code: str) -> None:
            assert error_code == "blockedbyclient"
            self.aborted = True

    async def exercise() -> tuple[FakeRoute, BrowserNetworkAudit]:
        context = FakeContext()
        audit = BrowserNetworkAudit()
        await install_browser_network_policy(
            context,
            audit,
            resolve_target=lambda _url: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        route = FakeRoute()
        await context.handler(
            route,
            SimpleNamespace(
                url="https://www.researchgate.net/publication/1",
                resource_type="document",
                is_navigation_request=lambda: True,
                redirected_from=None,
                frame=SimpleNamespace(url="about:blank"),
            ),
        )
        return route, audit

    route, audit = asyncio.run(exercise())

    assert route.aborted is True
    assert audit.blocked_navigation == 1
    assert audit.events[0]["reason"] == "policy_error:RuntimeError"


def test_websocket_and_webrtc_guards_are_installed_before_pages() -> None:
    class FakeContext:
        web_socket_handler: Any = None
        init_script = ""
        calls: list[str]

        def __init__(self) -> None:
            self.calls = []

        async def add_init_script(self, *, script: str) -> None:
            self.init_script = script
            self.calls.append("init_script")

        async def route_web_socket(self, pattern: str, handler: Any) -> None:
            assert pattern == "**/*"
            self.web_socket_handler = handler
            self.calls.append("websocket_route")

        async def route(self, pattern: str, _handler: Any) -> None:
            assert pattern == "**/*"
            self.calls.append("http_route")

    class FakeWebSocket:
        url = "wss://www.researchgate.net/live"
        closed = False

        async def close(self, *, code: int, reason: str) -> None:
            assert code == 1008
            assert reason == "Blocked by network policy"
            self.closed = True

    async def exercise() -> tuple[FakeContext, FakeWebSocket, BrowserNetworkAudit]:
        context = FakeContext()
        audit = BrowserNetworkAudit()
        await install_browser_network_policy(context, audit, resolve_target=_public)
        web_socket = FakeWebSocket()
        await context.web_socket_handler(web_socket)
        return context, web_socket, audit

    context, web_socket, audit = asyncio.run(exercise())

    assert context.calls == ["init_script", "websocket_route", "http_route"]
    assert "WebSocket" in context.init_script
    assert "RTCPeerConnection" in context.init_script
    assert web_socket.closed is True
    assert audit.blocked == 1
    assert audit.events[0]["reason"] == "websocket_disabled"
