"""Destination checks for website runs.

Call ``validate_target_url`` before the first request and again for every
redirect destination. A hostname lookup check alone cannot prevent DNS
rebinding; the eventual runner must also pin/verify the connection address or
run behind network egress controls.

One exception exists: ``trusted_origin`` lets server-side code (PayRecover's order
investigation) test the store the operator configured, even on a private address.
It is set only from TESTPILOT_STORE_URL, never from a request, and covers that
exact scheme, host and port only.
"""

import asyncio
import ipaddress
import socket
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

_trusted_origin: ContextVar[tuple[str, str, int] | None] = ContextVar("trusted_origin", default=None)


def _origin(scheme: str, hostname: str, port: int | None) -> tuple[str, str, int]:
    return scheme, hostname, port or (443 if scheme == "https" else 80)


@contextmanager
def trusted_origin(url: str):
    """Allow private addresses for this one operator-configured origin within the block."""
    parsed = urlsplit(url)
    token = _trusted_origin.set(_origin(parsed.scheme.lower(), (parsed.hostname or "").rstrip(".").lower(), parsed.port))
    try:
        yield
    finally:
        _trusted_origin.reset(token)


class UnsafeTarget(ValueError):
    """The target is invalid or resolves outside the public address space."""


async def validate_target_url(url: str) -> tuple[str, ...]:
    """Validate an HTTP(S) URL and return its public resolved IP addresses.

    This is a preflight policy check, not a substitute for enforcing the
    destination at connection time.
    """
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        # Accessing .port also validates malformed and out-of-range ports.
        _ = parsed.port
    except ValueError as exc:
        raise UnsafeTarget("URL is malformed") from exc

    if scheme not in {"http", "https"} or not hostname:
        raise UnsafeTarget("only absolute HTTP or HTTPS URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeTarget("URLs containing embedded credentials are not allowed")
    if parsed.fragment:
        raise UnsafeTarget("URL fragments are not allowed")

    hostname = hostname.rstrip(".").lower()
    trusted = _trusted_origin.get() == _origin(scheme, hostname, parsed.port)
    if not trusted and (
        not hostname
        or "%" in hostname
        or hostname == "localhost"
        or hostname.endswith(".localhost")
    ):
        raise UnsafeTarget("local hostnames are not allowed")

    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            answers = await asyncio.wait_for(
                asyncio.to_thread(
                    socket.getaddrinfo,
                    hostname,
                    parsed.port or (443 if scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                ),
                timeout=5,
            )
        except (OSError, socket.gaierror) as exc:
            raise UnsafeTarget("hostname could not be resolved") from exc
        except TimeoutError as exc:
            raise UnsafeTarget("hostname lookup exceeded the 5 second limit") from exc
        addresses = tuple(sorted({answer[4][0].split("%")[0] for answer in answers}))
    else:
        addresses = (str(literal),)

    if not addresses:
        raise UnsafeTarget("hostname did not resolve to an address")

    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise UnsafeTarget("hostname resolved to an invalid address") from exc
        if not ip.is_global and not trusted:
            raise UnsafeTarget("target resolves to a non-public IP address")

    return addresses
