import ipaddress
import socket
from collections.abc import Callable, Iterable
from urllib.parse import SplitResult, urlsplit

from frame_worker.ingestion.errors import UnsafeVideoURLError

AddressResolver = Callable[[str, int], Iterable[str]]


def validate_safe_http_url(
    url: str,
    address_resolver: AddressResolver | None = None,
) -> SplitResult:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise UnsafeVideoURLError("Video URL is malformed") from error

    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeVideoURLError("Video URL must use http or https")
    if not parsed.hostname:
        raise UnsafeVideoURLError("Video URL must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeVideoURLError("Video URL credentials are not allowed")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise UnsafeVideoURLError("Localhost video URLs are not allowed")

    effective_port = port or (443 if parsed.scheme.lower() == "https" else 80)
    resolver = address_resolver or _resolve_addresses
    try:
        addresses = list(resolver(hostname, effective_port))
    except OSError as error:
        raise UnsafeVideoURLError("Video URL hostname could not be resolved") from error
    if not addresses:
        raise UnsafeVideoURLError("Video URL hostname did not resolve to an address")

    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as error:
            raise UnsafeVideoURLError(
                "Video URL resolved to an invalid address"
            ) from error
        if not address.is_global:
            raise UnsafeVideoURLError(
                "Video URL resolves to a non-public network address"
            )
    return parsed


def _resolve_addresses(hostname: str, port: int) -> set[str]:
    return {
        result[4][0]
        for result in socket.getaddrinfo(
            hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    }


def safe_url_label(url: str) -> str:
    parsed = urlsplit(url)
    return parsed.hostname or "unknown-host"
