from __future__ import annotations

import asyncio
import importlib
import ipaddress
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import Any, Callable, Iterable, cast

from .package_paths import ensure_local_package_paths


ResolveTarget = Callable[[str], Iterable[Any]]
_RESEARCHGATE_SUFFIX = "researchgate.net"
_RESEARCHGATE_CDN_SUFFIX = "rgstatic.net"
_MAX_AUDIT_EVENTS = 256
_BROWSER_NETWORK_GUARD_SCRIPT = """
(() => {
  class BlockedWebSocket {
    constructor() { throw new DOMException('WebSocket disabled by network policy', 'SecurityError'); }
  }
  class BlockedPeerConnection {
    constructor() { throw new DOMException('WebRTC disabled by network policy', 'SecurityError'); }
  }
  for (const [name, value] of [
    ['WebSocket', BlockedWebSocket],
    ['RTCPeerConnection', BlockedPeerConnection],
    ['webkitRTCPeerConnection', BlockedPeerConnection],
  ]) {
    try {
      Object.defineProperty(globalThis, name, {
        value,
        configurable: false,
        enumerable: false,
        writable: false,
      });
    } catch (_) {}
  }
})();
"""


@dataclass(frozen=True)
class BrowserRequestDecision:
    allowed: bool
    reason: str
    category: str
    url: str
    host: str = ""
    resolved_addresses: tuple[str, ...] = ()
    resource_type: str = ""
    navigation: bool = False

    def to_audit_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["url"] = redact_network_url(self.url)
        return payload


@dataclass
class BrowserNetworkAudit:
    max_events: int = _MAX_AUDIT_EVENTS
    allowed: int = 0
    blocked: int = 0
    allowed_navigation: int = 0
    blocked_navigation: int = 0
    omitted_events: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)

    def record(self, decision: BrowserRequestDecision) -> None:
        if decision.allowed:
            self.allowed += 1
            if decision.navigation:
                self.allowed_navigation += 1
        else:
            self.blocked += 1
            if decision.navigation:
                self.blocked_navigation += 1

        observable = (
            not decision.allowed
            or decision.navigation
            or decision.category in {"researchgate_cdn", "external_public_subresource"}
        )
        if not observable:
            return
        if len(self.events) >= max(1, int(self.max_events)):
            self.omitted_events += 1
            return
        self.events.append(decision.to_audit_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "blocked": self.blocked,
            "allowed_navigation": self.allowed_navigation,
            "blocked_navigation": self.blocked_navigation,
            "omitted_events": self.omitted_events,
            "events": list(self.events),
        }


def validate_researchgate_initial_url(
    url: str,
    *,
    resolve_target: ResolveTarget | None = None,
) -> BrowserRequestDecision:
    return evaluate_browser_request(
        url,
        resource_type="document",
        navigation=True,
        initial=True,
        resolve_target=resolve_target,
    )


def evaluate_browser_request(
    url: str,
    *,
    resource_type: str,
    navigation: bool,
    redirected_from_url: str = "",
    initiator_url: str = "",
    initial: bool = False,
    resolve_target: ResolveTarget | None = None,
) -> BrowserRequestDecision:
    raw_url = str(url or "")
    parsed = _parse_url(raw_url)
    scheme = parsed.scheme.casefold()
    normalized_resource_type = str(resource_type or "").casefold()

    if scheme in {"about", "data", "blob"}:
        return _evaluate_non_network_url(
            raw_url,
            parsed=parsed,
            resource_type=normalized_resource_type,
            navigation=navigation,
            initiator_url=initiator_url,
        )
    if scheme != "https":
        return _blocked(
            raw_url,
            reason="https_required",
            resource_type=normalized_resource_type,
            navigation=navigation,
        )
    if parsed.username is not None or parsed.password is not None:
        return _blocked(
            raw_url,
            reason="credentials_forbidden",
            resource_type=normalized_resource_type,
            navigation=navigation,
        )
    try:
        port = parsed.port
    except ValueError:
        return _blocked(
            raw_url,
            reason="invalid_port",
            resource_type=normalized_resource_type,
            navigation=navigation,
        )
    if port not in {None, 443}:
        return _blocked(
            raw_url,
            reason="nonstandard_port",
            resource_type=normalized_resource_type,
            navigation=navigation,
        )

    host = _normalized_host(parsed.hostname)
    if not host:
        return _blocked(
            raw_url,
            reason="missing_host",
            resource_type=normalized_resource_type,
            navigation=navigation,
        )
    if initial and not _host_matches(host, _RESEARCHGATE_SUFFIX):
        return _blocked(
            raw_url,
            reason="initial_host_not_researchgate",
            host=host,
            resource_type=normalized_resource_type,
            navigation=navigation,
        )

    addresses, address_error = _resolve_public_addresses(
        raw_url,
        resolve_target=resolve_target,
    )
    if address_error:
        return _blocked(
            raw_url,
            reason=address_error,
            host=host,
            resolved_addresses=addresses,
            resource_type=normalized_resource_type,
            navigation=navigation,
        )

    if navigation or normalized_resource_type == "document":
        category = _navigation_category(
            raw_url,
            host=host,
            redirected_from_url=redirected_from_url,
            initiator_url=initiator_url,
            initial=initial,
        )
        if category is None:
            return _blocked(
                raw_url,
                reason="cross_host_navigation_blocked",
                host=host,
                resolved_addresses=addresses,
                resource_type=normalized_resource_type,
                navigation=True,
            )
    elif _host_matches(host, _RESEARCHGATE_SUFFIX):
        category = "researchgate_subresource"
    elif _host_matches(host, _RESEARCHGATE_CDN_SUFFIX):
        category = "researchgate_cdn"
    else:
        category = "external_public_subresource"

    return BrowserRequestDecision(
        True,
        "allowed",
        category,
        raw_url,
        host=host,
        resolved_addresses=addresses,
        resource_type=normalized_resource_type,
        navigation=bool(navigation or normalized_resource_type == "document"),
    )


async def install_browser_network_policy(
    context: Any,
    audit: BrowserNetworkAudit,
    *,
    resolve_target: ResolveTarget | None = None,
) -> Callable[..., Any]:
    async def route_handler(route: Any, request: Any) -> None:
        url = str(getattr(request, "url", "") or "")
        resource_type = str(getattr(request, "resource_type", "") or "")
        navigation_value = getattr(request, "is_navigation_request", False)
        navigation = bool(
            navigation_value() if callable(navigation_value) else navigation_value
        )
        redirected_from = getattr(request, "redirected_from", None)
        redirected_from_url = str(getattr(redirected_from, "url", "") or "")
        initiator_url = _request_frame_url(request)
        try:
            decision = await asyncio.to_thread(
                evaluate_browser_request,
                url,
                resource_type=resource_type,
                navigation=navigation,
                redirected_from_url=redirected_from_url,
                initiator_url=initiator_url,
                resolve_target=resolve_target,
            )
        except Exception as exc:  # noqa: BLE001 - policy failures must fail closed.
            decision = _blocked(
                url,
                reason=f"policy_error:{type(exc).__name__}",
                resource_type=resource_type,
                navigation=navigation,
            )
        audit.record(decision)
        if decision.allowed:
            await route.continue_()
        else:
            await route.abort(error_code="blockedbyclient")

    add_init_script = getattr(context, "add_init_script", None)
    if callable(add_init_script):
        await add_init_script(script=_BROWSER_NETWORK_GUARD_SCRIPT)

    route_web_socket = getattr(context, "route_web_socket", None)
    if callable(route_web_socket):
        async def web_socket_handler(web_socket: Any) -> None:
            decision = _blocked(
                str(getattr(web_socket, "url", "") or ""),
                reason="websocket_disabled",
                resource_type="websocket",
                navigation=False,
            )
            audit.record(decision)
            await web_socket.close(code=1008, reason="Blocked by network policy")

        await route_web_socket("**/*", web_socket_handler)

    await context.route("**/*", route_handler)
    return route_handler


def _navigation_category(
    url: str,
    *,
    host: str,
    redirected_from_url: str,
    initiator_url: str,
    initial: bool,
) -> str | None:
    if _host_matches(host, _RESEARCHGATE_SUFFIX):
        return "researchgate_navigation"
    if not initial and _host_matches(host, _RESEARCHGATE_CDN_SUFFIX):
        return "researchgate_cdn"
    source_host = _url_host(redirected_from_url) or _url_host(initiator_url)
    if (
        not initial
        and _host_in_researchgate_family(source_host)
        and _looks_like_pdf_download(url)
    ):
        return "external_pdf_download"
    return None


def _evaluate_non_network_url(
    url: str,
    *,
    parsed: urllib.parse.SplitResult,
    resource_type: str,
    navigation: bool,
    initiator_url: str,
) -> BrowserRequestDecision:
    scheme = parsed.scheme.casefold()
    if navigation or resource_type == "document":
        return _blocked(
            url,
            reason="non_network_navigation_blocked",
            resource_type=resource_type,
            navigation=True,
        )
    if scheme == "about" and url.casefold() != "about:blank":
        return _blocked(
            url,
            reason="about_url_blocked",
            resource_type=resource_type,
            navigation=False,
        )
    if scheme == "blob":
        blob_origin = url[5:]
        origin_host = _url_host(blob_origin)
        initiator_host = _url_host(initiator_url)
        if not _host_in_researchgate_family(origin_host) or not _host_in_researchgate_family(
            initiator_host
        ):
            return _blocked(
                url,
                reason="untrusted_blob_origin",
                resource_type=resource_type,
                navigation=False,
            )
    return BrowserRequestDecision(
        True,
        "allowed_non_network_resource",
        f"{scheme}_resource",
        url,
        resource_type=resource_type,
        navigation=False,
    )


def _resolve_public_addresses(
    url: str,
    *,
    resolve_target: ResolveTarget | None,
) -> tuple[tuple[str, ...], str]:
    resolver = resolve_target or _default_resolve_target
    try:
        resolved = tuple(resolver(url))
    except OSError as exc:
        return (), f"dns_error:{type(exc).__name__}"
    except ValueError as exc:
        return (), f"unsafe_target:{type(exc).__name__}"
    if not resolved:
        return (), "dns_empty"

    addresses: list[str] = []
    for address in resolved:
        value = str(getattr(address, "ip", address)).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            return tuple(addresses), "invalid_resolved_address"
        addresses.append(str(ip))
        if not ip.is_global:
            return tuple(addresses), "blocked_resolved_address"
    return tuple(dict.fromkeys(addresses)), ""


def _default_resolve_target(url: str) -> Iterable[Any]:
    ensure_local_package_paths()
    module = importlib.import_module("zotero_metadata_enrichment.safe_http")
    resolver = cast(ResolveTarget, getattr(module, "resolve_safe_http_target"))
    return resolver(url)


def _parse_url(url: str) -> urllib.parse.SplitResult:
    if not url or url != url.strip():
        return urllib.parse.SplitResult("", "", "", "", "")
    if "\\" in url or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in url):
        return urllib.parse.SplitResult("", "", "", "", "")
    try:
        return urllib.parse.urlsplit(url)
    except ValueError:
        return urllib.parse.SplitResult("", "", "", "", "")


def _blocked(
    url: str,
    *,
    reason: str,
    host: str = "",
    resolved_addresses: tuple[str, ...] = (),
    resource_type: str = "",
    navigation: bool,
) -> BrowserRequestDecision:
    return BrowserRequestDecision(
        False,
        reason,
        "blocked",
        url,
        host=host,
        resolved_addresses=resolved_addresses,
        resource_type=resource_type,
        navigation=navigation,
    )


def _request_frame_url(request: Any) -> str:
    try:
        frame = getattr(request, "frame", None)
        return str(getattr(frame, "url", "") or "")
    except Exception:
        return ""


def _looks_like_pdf_download(url: str) -> bool:
    parsed = _parse_url(url)
    path = urllib.parse.unquote(parsed.path or "").casefold()
    filename = PurePosixPath(path).name
    query = urllib.parse.parse_qs(parsed.query.casefold(), keep_blank_values=True)
    return (
        filename.endswith(".pdf")
        or "download" in path
        or "pdf" in query.get("format", ())
        or any("attachment" in value for value in query.get("response-content-disposition", ()))
    )


def _url_host(url: str) -> str:
    parsed = _parse_url(str(url or ""))
    return _normalized_host(parsed.hostname)


def _normalized_host(host: str | None) -> str:
    return str(host or "").strip().strip(".").casefold()


def _host_matches(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith(f".{suffix}")


def _host_in_researchgate_family(host: str) -> bool:
    return _host_matches(host, _RESEARCHGATE_SUFFIX) or _host_matches(
        host,
        _RESEARCHGATE_CDN_SUFFIX,
    )


def redact_network_url(url: str) -> str:
    parsed = _parse_url(str(url or ""))
    if not parsed.scheme:
        return "[invalid-url]"
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if port is not None else host
    query = "[redacted]" if parsed.query else ""
    return urllib.parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path, query, "")
    )
