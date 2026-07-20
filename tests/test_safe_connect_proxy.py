from __future__ import annotations

import asyncio
import urllib.parse
from types import SimpleNamespace

from zotero_ingest_worker.safe_connect_proxy import (
    PinnedProxyAddress,
    SafeConnectProxy,
)


async def _read_response_headers(
    reader: asyncio.StreamReader,
) -> list[bytes]:
    lines: list[bytes] = []
    while True:
        line = await reader.readline()
        if line in {b"", b"\r\n"}:
            return lines
        lines.append(line.rstrip(b"\r\n"))


def _proxy_endpoint(proxy: SafeConnectProxy) -> tuple[str, int]:
    parsed = urllib.parse.urlsplit(proxy.server_url)
    assert parsed.hostname is not None
    assert parsed.port is not None
    return parsed.hostname, parsed.port


def test_safe_connect_proxy_blocks_private_resolution_before_dial() -> None:
    dial_calls: list[PinnedProxyAddress] = []

    async def unexpected_dial(
        address: PinnedProxyAddress,
        _timeout: float,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        dial_calls.append(address)
        raise AssertionError("Private address must not be dialed.")

    async def scenario() -> bytes:
        async with SafeConnectProxy(
            resolve_target=lambda _url: [SimpleNamespace(ip="127.0.0.1")],
            dial_target=unexpected_dial,
        ) as proxy:
            reader, writer = await asyncio.open_connection(*_proxy_endpoint(proxy))
            writer.write(
                b"CONNECT example.test:443 HTTP/1.1\r\nHost: example.test:443\r\n\r\n"
            )
            await writer.drain()
            status = await reader.readline()
            writer.close()
            await writer.wait_closed()
            return status

    status = asyncio.run(scenario())

    assert status.startswith(b"HTTP/1.1 403")
    assert dial_calls == []


def test_safe_connect_proxy_dials_only_validated_public_address() -> None:
    dial_calls: list[PinnedProxyAddress] = []

    async def scenario() -> tuple[bytes, bytes]:
        async def upstream_handler(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            payload = await reader.readexactly(4)
            writer.write(payload.upper())
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        upstream = await asyncio.start_server(
            upstream_handler,
            host="127.0.0.1",
            port=0,
        )
        assert upstream.sockets
        upstream_host, upstream_port = upstream.sockets[0].getsockname()[:2]

        async def fake_dial(
            address: PinnedProxyAddress,
            _timeout: float,
        ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            dial_calls.append(address)
            return await asyncio.open_connection(upstream_host, upstream_port)

        try:
            async with SafeConnectProxy(
                resolve_target=lambda _url: [SimpleNamespace(ip="93.184.216.34")],
                dial_target=fake_dial,
            ) as proxy:
                reader, writer = await asyncio.open_connection(*_proxy_endpoint(proxy))
                writer.write(
                    b"CONNECT example.test:443 HTTP/1.1\r\n"
                    b"Host: example.test:443\r\n\r\n"
                )
                await writer.drain()
                status = await reader.readline()
                await _read_response_headers(reader)
                writer.write(b"ping")
                await writer.drain()
                echoed = await reader.readexactly(4)
                writer.close()
                await writer.wait_closed()
                return status, echoed
        finally:
            upstream.close()
            await upstream.wait_closed()

    status, echoed = asyncio.run(scenario())

    assert status.startswith(b"HTTP/1.1 200")
    assert echoed == b"PING"
    assert [address.ip for address in dial_calls] == ["93.184.216.34"]


def test_safe_connect_proxy_rejects_nonstandard_connect_port() -> None:
    resolve_calls: list[str] = []

    async def scenario() -> bytes:
        async with SafeConnectProxy(
            resolve_target=lambda url: (
                resolve_calls.append(url) or [SimpleNamespace(ip="93.184.216.34")]
            ),
        ) as proxy:
            reader, writer = await asyncio.open_connection(*_proxy_endpoint(proxy))
            writer.write(
                b"CONNECT example.test:444 HTTP/1.1\r\nHost: example.test:444\r\n\r\n"
            )
            await writer.drain()
            status = await reader.readline()
            writer.close()
            await writer.wait_closed()
            return status

    status = asyncio.run(scenario())

    assert status.startswith(b"HTTP/1.1 400")
    assert resolve_calls == []
