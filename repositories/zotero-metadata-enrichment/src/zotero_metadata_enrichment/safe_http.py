from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import IO, Any, Callable, cast


RedirectValidator = Callable[[str, str], bool]
BeforeOpen = Callable[[str], None]
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_SENSITIVE_HEADERS = {
    "api-key",
    "authorization",
    "cookie",
    "proxy-authorization",
    "x-api-key",
}
_BODY_HEADERS = {"content-length", "content-type", "transfer-encoding"}


class UnsafeUrlError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        source_url: str = "",
        target_url: str = "",
    ) -> None:
        super().__init__(message)
        self.source_url = source_url
        self.target_url = target_url

    @property
    def is_redirect(self) -> bool:
        return bool(self.source_url and self.target_url)


@dataclass(frozen=True)
class ResolvedAddress:
    family: int
    socktype: int
    proto: int
    sockaddr: tuple[Any, ...]
    ip: str


class SafeHttpResponse:
    def __init__(
        self,
        response: http.client.HTTPResponse,
        connection: http.client.HTTPConnection,
        *,
        url: str,
    ) -> None:
        self._response = response
        self._connection = connection
        self.url = url
        self.status = response.status
        self.code = response.status
        self.reason = response.reason
        self.headers = response.headers

    def read(self, amount: int | None = None) -> bytes:
        return self._response.read() if amount is None else self._response.read(amount)

    def geturl(self) -> str:
        return self.url

    def getcode(self) -> int:
        return self.status

    def info(self) -> Any:
        return self.headers

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._connection.close()

    def __enter__(self) -> SafeHttpResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def safe_urlopen(
    request: urllib.request.Request | str,
    *,
    timeout: float,
    redirect_validator: RedirectValidator | None = None,
    max_redirects: int = 5,
    allow_private_networks: bool = False,
    allow_loopback: bool = False,
    before_open: BeforeOpen | None = None,
) -> SafeHttpResponse:
    current = _coerce_request(request)
    redirect_count = 0
    while True:
        addresses = getattr(current, "_zotero_resolved_addresses", None)
        if not addresses:
            addresses = _resolve_target(
                current.full_url,
                allow_private_networks=allow_private_networks,
                allow_loopback=allow_loopback,
            )
        setattr(current, "_zotero_resolved_addresses", addresses)
        if before_open is not None:
            before_open(current.full_url)
        response = _open_pinned_once(current, timeout=timeout)
        status = _response_status(response)
        location = _response_location(response) if status in _REDIRECT_STATUSES else ""
        if not location:
            if status >= 300:
                raise urllib.error.HTTPError(
                    current.full_url,
                    status,
                    str(getattr(response, "reason", "HTTP error")),
                    response.headers,
                    cast(IO[bytes], response),
                )
            return response

        target_url = urllib.parse.urljoin(current.full_url, location)
        if redirect_count >= max(0, int(max_redirects)):
            _close_response(response)
            raise UnsafeUrlError(
                f"Too many redirects for {current.full_url}",
                source_url=current.full_url,
                target_url=target_url,
            )

        _close_response(response)
        _validate_redirect(
            current.full_url,
            target_url,
            redirect_validator,
            allow_local_hostname=allow_loopback,
        )
        try:
            target_addresses = _resolve_target(
                target_url,
                allow_private_networks=allow_private_networks,
                allow_loopback=allow_loopback,
            )
        except UnsafeUrlError as exc:
            raise UnsafeUrlError(
                str(exc),
                source_url=current.full_url,
                target_url=target_url,
            ) from exc
        current = _redirect_request(current, target_url, status)
        setattr(current, "_zotero_resolved_addresses", target_addresses)
        redirect_count += 1


def same_host_redirect(source_url: str, target_url: str) -> bool:
    return _url_host(source_url) == _url_host(target_url)


def host_suffix_redirect(*suffixes: str) -> RedirectValidator:
    normalized = tuple(str(value).strip(".").casefold() for value in suffixes if value)

    def validator(source_url: str, target_url: str) -> bool:
        return _host_matches(_url_host(source_url), normalized) and _host_matches(
            _url_host(target_url), normalized
        )

    return validator


def _validate_redirect(
    source_url: str,
    target_url: str,
    redirect_validator: RedirectValidator | None,
    *,
    allow_local_hostname: bool = False,
) -> None:
    try:
        source = _parse_http_url(source_url, allow_local_hostname=allow_local_hostname)
        target = _parse_http_url(target_url, allow_local_hostname=allow_local_hostname)
    except UnsafeUrlError as exc:
        raise UnsafeUrlError(
            str(exc),
            source_url=source_url,
            target_url=target_url,
        ) from exc
    if source.scheme.casefold() == "https" and target.scheme.casefold() != "https":
        raise UnsafeUrlError(
            f"HTTPS downgrade redirect rejected: {source_url} -> {target_url}",
            source_url=source_url,
            target_url=target_url,
        )
    if redirect_validator is not None and not redirect_validator(source_url, target_url):
        raise UnsafeUrlError(
            f"Redirect policy rejected: {source_url} -> {target_url}",
            source_url=source_url,
            target_url=target_url,
        )


def _resolve_target(
    url: str,
    *,
    allow_private_networks: bool,
    allow_loopback: bool,
) -> tuple[ResolvedAddress, ...]:
    parsed = _parse_http_url(url, allow_local_hostname=allow_loopback)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
    try:
        records = socket.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except OSError:
        raise

    addresses: list[ResolvedAddress] = []
    seen: set[tuple[int, str, tuple[Any, ...]]] = set()
    for family, socktype, proto, _canonname, sockaddr in records:
        if family not in {socket.AF_INET, socket.AF_INET6} or not sockaddr:
            continue
        ip_text = str(sockaddr[0])
        try:
            ip = ipaddress.ip_address(ip_text.split("%", 1)[0])
        except ValueError as exc:
            raise UnsafeUrlError(f"Invalid resolved address for {host}: {ip_text}") from exc
        if not _ip_allowed(
            ip,
            allow_private_networks=allow_private_networks,
            allow_loopback=allow_loopback,
        ):
            raise UnsafeUrlError(f"Blocked resolved address for {host}: {ip_text}")
        normalized_sockaddr = tuple(sockaddr)
        key = (family, ip_text, normalized_sockaddr)
        if key in seen:
            continue
        seen.add(key)
        addresses.append(
            ResolvedAddress(
                family=family,
                socktype=socktype,
                proto=proto,
                sockaddr=normalized_sockaddr,
                ip=ip_text,
            )
        )
    if not addresses:
        raise OSError(f"No usable address resolved for {host}")
    return tuple(addresses)


def _ip_allowed(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    allow_private_networks: bool,
    allow_loopback: bool,
) -> bool:
    if ip.is_loopback:
        return allow_loopback
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return False
    if ip.is_private:
        return allow_private_networks
    return ip.is_global


def _open_pinned_once(request: urllib.request.Request, *, timeout: float) -> SafeHttpResponse:
    parsed = _parse_http_url(request.full_url, allow_local_hostname=True)
    addresses = getattr(request, "_zotero_resolved_addresses", ())
    if not addresses:
        raise UnsafeUrlError(f"Request has no pinned address: {request.full_url}")
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
    connection: http.client.HTTPConnection | None = None
    last_error: OSError | None = None
    for address in addresses:
        candidate: http.client.HTTPConnection | None = None
        try:
            if parsed.scheme.casefold() == "https":
                candidate = _PinnedHTTPSConnection(
                    host,
                    port,
                    address=address,
                    timeout=timeout,
                    context=ssl.create_default_context(),
                )
            else:
                candidate = _PinnedHTTPConnection(
                    host,
                    port,
                    address=address,
                    timeout=timeout,
                )
            candidate.connect()
            connection = candidate
            break
        except OSError as exc:
            last_error = exc
            if candidate is not None:
                candidate.close()
    if connection is None:
        if last_error is not None:
            raise last_error
        raise OSError(f"Unable to connect to {host}")

    headers = {
        str(name): str(value)
        for name, value in request.header_items()
        if str(name).casefold() != "host"
    }
    target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    try:
        connection.request(
            request.get_method(),
            target,
            body=request.data,
            headers=headers,
        )
        return SafeHttpResponse(connection.getresponse(), connection, url=request.full_url)
    except Exception:
        connection.close()
        raise


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(
        self,
        host: str,
        port: int,
        *,
        address: ResolvedAddress,
        timeout: float,
    ) -> None:
        super().__init__(host, port, timeout=timeout)
        self._resolved_address = address

    def connect(self) -> None:
        self.sock = _dial_resolved_address(self._resolved_address, self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        port: int,
        *,
        address: ResolvedAddress,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._resolved_address = address
        self._ssl_context = context

    def connect(self) -> None:
        raw_socket = _dial_resolved_address(self._resolved_address, self.timeout)
        try:
            self.sock = self._ssl_context.wrap_socket(raw_socket, server_hostname=self.host)
        except Exception:
            raw_socket.close()
            raise


def _dial_resolved_address(
    address: ResolvedAddress,
    timeout: float | None,
) -> socket.socket:
    sock = socket.socket(address.family, address.socktype, address.proto)
    try:
        sock.settimeout(timeout)
        sock.connect(address.sockaddr)
        peer_ip = str(sock.getpeername()[0]).split("%", 1)[0]
        expected_ip = str(address.ip).split("%", 1)[0]
        if ipaddress.ip_address(peer_ip) != ipaddress.ip_address(expected_ip):
            raise UnsafeUrlError(f"Connected peer changed: {expected_ip} -> {peer_ip}")
        return sock
    except Exception:
        sock.close()
        raise


def _coerce_request(request: urllib.request.Request | str) -> urllib.request.Request:
    if isinstance(request, urllib.request.Request):
        return _clone_request(request, request.full_url)
    return urllib.request.Request(str(request), method="GET")


def _clone_request(
    request: urllib.request.Request,
    url: str,
    *,
    method: str | None = None,
    data: Any = ...,
) -> urllib.request.Request:
    body = request.data if data is ... else data
    headers = {str(name): str(value) for name, value in request.header_items()}
    return urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method=method or request.get_method(),
    )


def _redirect_request(
    request: urllib.request.Request,
    target_url: str,
    status: int,
) -> urllib.request.Request:
    method = request.get_method().upper()
    data: Any = request.data
    if status == 303 and method != "HEAD" or status in {301, 302} and method == "POST":
        method = "GET"
        data = None
    redirected = _clone_request(request, target_url, method=method, data=data)
    if data is None:
        _remove_headers(redirected, _BODY_HEADERS)
    if _url_origin(request.full_url) != _url_origin(target_url):
        _remove_headers(redirected, _SENSITIVE_HEADERS)
    return redirected


def _remove_headers(request: urllib.request.Request, blocked: set[str]) -> None:
    for name, _value in tuple(request.header_items()):
        if name.casefold() in blocked:
            request.remove_header(name)


def _parse_http_url(
    url: str,
    *,
    allow_local_hostname: bool = False,
) -> urllib.parse.SplitResult:
    raw_url = str(url or "")
    if any(ord(character) <= 0x20 or ord(character) == 0x7F for character in raw_url):
        raise UnsafeUrlError(f"Control character or whitespace in URL rejected: {url}")
    if "\\" in raw_url:
        raise UnsafeUrlError(f"Backslash in URL rejected: {url}")
    try:
        parsed = urllib.parse.urlsplit(raw_url)
        _ = parsed.port
    except ValueError as exc:
        raise UnsafeUrlError(f"Invalid URL rejected: {url}") from exc
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise UnsafeUrlError(f"Unsupported URL scheme rejected: {url}")
    if not parsed.hostname:
        raise UnsafeUrlError(f"URL host is required: {url}")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError(f"Credentials in URL are forbidden: {url}")
    host = parsed.hostname.casefold()
    if not allow_local_hostname and (
        host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost")
    ):
        raise UnsafeUrlError(f"Local hostname rejected: {url}")
    return parsed


def _response_status(response: Any) -> int:
    status = getattr(response, "status", getattr(response, "code", 200))
    try:
        return int(status)
    except (TypeError, ValueError):
        return 200


def _response_location(response: Any) -> str:
    headers = getattr(response, "headers", None)
    if headers is None:
        return ""
    try:
        return str(headers.get("Location") or "").strip()
    except AttributeError:
        return ""


def _close_response(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def _url_host(url: str) -> str:
    return (_parse_http_url(url).hostname or "").casefold()


def _url_origin(url: str) -> tuple[str, str, int]:
    parsed = _parse_http_url(url)
    scheme = parsed.scheme.casefold()
    return scheme, (parsed.hostname or "").casefold(), parsed.port or (443 if scheme == "https" else 80)


def _host_matches(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in suffixes)
