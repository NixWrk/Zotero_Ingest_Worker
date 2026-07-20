from __future__ import annotations

import asyncio
import ipaddress
import socket
import urllib.parse
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .package_paths import ensure_local_package_paths

ensure_local_package_paths()

from zotero_metadata_enrichment.safe_http import resolve_safe_http_target  # noqa: E402


ResolveTarget = Callable[[str], Iterable[Any]]
DialTarget = Callable[
    ["PinnedProxyAddress", float],
    Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]],
]


@dataclass(frozen=True)
class PinnedProxyAddress:
    family: int
    socktype: int
    proto: int
    sockaddr: tuple[Any, ...]
    ip: str


class SafeConnectProxy:
    def __init__(
        self,
        *,
        resolve_target: ResolveTarget | None = None,
        dial_target: DialTarget | None = None,
        connect_timeout_seconds: float = 15.0,
        header_limit_bytes: int = 16_384,
    ) -> None:
        self._resolve_target = resolve_target or resolve_safe_http_target
        self._dial_target = dial_target or _dial_pinned_target
        self._connect_timeout_seconds = max(0.1, float(connect_timeout_seconds))
        self._header_limit_bytes = max(1024, int(header_limit_bytes))
        self._server: asyncio.Server | None = None
        self._handlers: set[asyncio.Task[Any]] = set()

    @property
    def server_url(self) -> str:
        server = self._server
        if server is None or not server.sockets:
            raise RuntimeError("Safe CONNECT proxy is not running.")
        host, port = server.sockets[0].getsockname()[:2]
        return f"http://{host}:{port}"

    async def start(self) -> SafeConnectProxy:
        if self._server is not None:
            return self
        self._server = await asyncio.start_server(
            self._handle_client,
            host="127.0.0.1",
            port=0,
            limit=self._header_limit_bytes + 1,
        )
        return self

    async def aclose(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            await server.wait_closed()
        current = asyncio.current_task()
        handlers = [
            task
            for task in tuple(self._handlers)
            if task is not current and not task.done()
        ]
        for task in handlers:
            task.cancel()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)

    async def __aenter__(self) -> SafeConnectProxy:
        return await self.start()

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def _handle_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
        try:
            await self._proxy_connect(client_reader, client_writer)
        except asyncio.CancelledError:
            raise
        except Exception:
            await _send_proxy_response(client_writer, 502, "Bad Gateway")
        finally:
            client_writer.close()
            try:
                await client_writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            if task is not None:
                self._handlers.discard(task)

    async def _proxy_connect(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        try:
            raw_request = await client_reader.readuntil(b"\r\n\r\n")
        except asyncio.LimitOverrunError:
            await _send_proxy_response(
                client_writer,
                431,
                "Request Header Fields Too Large",
            )
            return
        except asyncio.IncompleteReadError:
            await _send_proxy_response(client_writer, 400, "Bad Request")
            return
        if len(raw_request) > self._header_limit_bytes:
            await _send_proxy_response(
                client_writer,
                431,
                "Request Header Fields Too Large",
            )
            return

        try:
            first_line = raw_request.decode("iso-8859-1").split("\r\n", 1)[0]
            method, authority, version = first_line.split(" ", 2)
            if method != "CONNECT" or not version.startswith("HTTP/"):
                raise ValueError("CONNECT required")
            host, port = _parse_connect_authority(authority)
        except (UnicodeError, ValueError):
            await _send_proxy_response(client_writer, 400, "Bad Request")
            return

        target_url = _target_url(host, port)
        try:
            raw_addresses = await asyncio.to_thread(
                lambda: tuple(self._resolve_target(target_url))
            )
            addresses = _normalize_public_addresses(raw_addresses, port=port)
        except OSError:
            await _send_proxy_response(client_writer, 502, "Bad Gateway")
            return
        except ValueError:
            await _send_proxy_response(client_writer, 403, "Forbidden")
            return

        upstream_reader: asyncio.StreamReader | None = None
        upstream_writer: asyncio.StreamWriter | None = None
        for address in addresses:
            try:
                upstream_reader, upstream_writer = await self._dial_target(
                    address,
                    self._connect_timeout_seconds,
                )
                break
            except (ConnectionError, OSError, TimeoutError):
                continue
        if upstream_reader is None or upstream_writer is None:
            await _send_proxy_response(client_writer, 502, "Bad Gateway")
            return

        try:
            await _send_proxy_response(
                client_writer,
                200,
                "Connection Established",
                keep_open=True,
            )
            await _tunnel(
                client_reader,
                client_writer,
                upstream_reader,
                upstream_writer,
            )
        finally:
            upstream_writer.close()
            try:
                await upstream_writer.wait_closed()
            except (ConnectionError, OSError):
                pass


def _parse_connect_authority(authority: str) -> tuple[str, int]:
    if (
        not authority
        or authority != authority.strip()
        or "\\" in authority
        or any(
            ord(character) <= 0x20 or ord(character) == 0x7F for character in authority
        )
    ):
        raise ValueError("Invalid CONNECT authority")
    try:
        parsed = urllib.parse.urlsplit(f"//{authority}")
        port = parsed.port or 443
    except ValueError as exc:
        raise ValueError("Invalid CONNECT authority") from exc
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or port != 443
    ):
        raise ValueError("Only HTTPS CONNECT targets are allowed")
    return parsed.hostname, port


def _target_url(host: str, port: int) -> str:
    authority = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"https://{authority}:{port}/"


def _normalize_public_addresses(
    values: Iterable[Any],
    *,
    port: int,
) -> tuple[PinnedProxyAddress, ...]:
    addresses: list[PinnedProxyAddress] = []
    seen: set[tuple[int, str]] = set()
    for value in values:
        ip_text = str(getattr(value, "ip", value)).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError as exc:
            raise ValueError("Resolver returned an invalid address") from exc
        if not ip.is_global:
            raise ValueError("Resolver returned a non-public address")
        family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
        key = family, str(ip)
        if key in seen:
            continue
        seen.add(key)
        raw_sockaddr: tuple[Any, ...] = tuple(getattr(value, "sockaddr", ()))
        scope_id = (
            int(raw_sockaddr[3])
            if family == socket.AF_INET6 and len(raw_sockaddr) > 3
            else 0
        )
        sockaddr: tuple[Any, ...]
        if family == socket.AF_INET6:
            sockaddr = (str(ip), port, 0, scope_id)
        else:
            sockaddr = (str(ip), port)
        addresses.append(
            PinnedProxyAddress(
                family=family,
                socktype=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
                sockaddr=sockaddr,
                ip=str(ip),
            )
        )
    if not addresses:
        raise OSError("Resolver returned no usable public address")
    return tuple(addresses)


async def _dial_pinned_target(
    address: PinnedProxyAddress,
    timeout_seconds: float,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    sock = socket.socket(address.family, address.socktype, address.proto)
    sock.setblocking(False)
    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().sock_connect(sock, address.sockaddr),
            timeout=max(0.1, float(timeout_seconds)),
        )
        peer_ip = str(sock.getpeername()[0]).split("%", 1)[0]
        if ipaddress.ip_address(peer_ip) != ipaddress.ip_address(address.ip):
            raise OSError(f"Pinned proxy peer changed: {address.ip} -> {peer_ip}")
        return await asyncio.open_connection(sock=sock)
    except BaseException:
        sock.close()
        raise


async def _send_proxy_response(
    writer: asyncio.StreamWriter,
    status: int,
    reason: str,
    *,
    keep_open: bool = False,
) -> None:
    connection = "keep-alive" if keep_open else "close"
    response = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Connection: {connection}\r\n"
        "Content-Length: 0\r\n"
        "\r\n"
    ).encode("ascii")
    try:
        writer.write(response)
        await writer.drain()
    except (ConnectionError, OSError):
        pass


async def _tunnel(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
) -> None:
    async def copy(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        while True:
            chunk = await reader.read(64 * 1024)
            if not chunk:
                return
            writer.write(chunk)
            await writer.drain()

    tasks = {
        asyncio.create_task(copy(client_reader, upstream_writer)),
        asyncio.create_task(copy(upstream_reader, client_writer)),
    }
    done, pending = await asyncio.wait(
        tasks,
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    await asyncio.gather(*done, *pending, return_exceptions=True)
