from __future__ import annotations

import ipaddress
import socket
import urllib.parse
from dataclasses import asdict, dataclass


BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain"}
BLOCKED_SCHEMES = {"file", "ftp", "gopher", "javascript", "data", "mailto"}


@dataclass(frozen=True)
class UrlSafetyResult:
    ok: bool
    reason: str = ""
    url: str = ""
    host: str = ""
    resolved_addresses: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def validate_fetch_url(
    url: str,
    *,
    resolve_dns: bool = False,
    allow_private_networks: bool = False,
) -> UrlSafetyResult:
    text = str(url or "").strip()
    if not text:
        return UrlSafetyResult(False, "empty_url", url=text)
    parsed = urllib.parse.urlparse(text)
    scheme = parsed.scheme.casefold()
    if scheme in BLOCKED_SCHEMES:
        return UrlSafetyResult(False, "blocked_scheme", url=text)
    if scheme not in {"http", "https"}:
        return UrlSafetyResult(False, "unsupported_scheme", url=text)
    host = (parsed.hostname or "").strip().casefold()
    if not host:
        return UrlSafetyResult(False, "missing_host", url=text)
    if host in BLOCKED_HOSTNAMES or host.endswith(".localhost"):
        return UrlSafetyResult(False, "blocked_host", url=text, host=host)

    literal_ip = _parse_ip(host)
    if literal_ip is not None and _is_blocked_ip(literal_ip, allow_private_networks=allow_private_networks):
        return UrlSafetyResult(False, "blocked_ip", url=text, host=host, resolved_addresses=(str(literal_ip),))

    if resolve_dns:
        try:
            addresses = tuple(sorted(_resolve_host_addresses(host)))
        except OSError as exc:
            return UrlSafetyResult(False, f"dns_error:{exc.__class__.__name__}", url=text, host=host)
        for address in addresses:
            ip = _parse_ip(address)
            if ip is not None and _is_blocked_ip(ip, allow_private_networks=allow_private_networks):
                return UrlSafetyResult(False, "blocked_resolved_ip", url=text, host=host, resolved_addresses=addresses)
        return UrlSafetyResult(True, url=text, host=host, resolved_addresses=addresses)

    return UrlSafetyResult(True, url=text, host=host)


def assert_fetch_url_safe(
    url: str,
    *,
    resolve_dns: bool = False,
    allow_private_networks: bool = False,
) -> UrlSafetyResult:
    result = validate_fetch_url(
        url,
        resolve_dns=resolve_dns,
        allow_private_networks=allow_private_networks,
    )
    if not result.ok:
        raise ValueError(f"Unsafe fetch URL rejected: {result.reason}: {url}")
    return result


def _parse_ip(host: str) -> ipaddress._BaseAddress | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def _resolve_host_addresses(host: str) -> set[str]:
    addresses: set[str] = set()
    for family, _type, _proto, _canonname, sockaddr in socket.getaddrinfo(host, None):
        if family in {socket.AF_INET, socket.AF_INET6} and sockaddr:
            addresses.add(str(sockaddr[0]))
    return addresses


def _is_blocked_ip(ip: ipaddress._BaseAddress, *, allow_private_networks: bool) -> bool:
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return True
    if ip.is_private and not allow_private_networks:
        return True
    return False
