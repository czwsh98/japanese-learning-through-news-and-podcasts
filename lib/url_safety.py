"""Safe, size-bounded HTTP fetching for user-provided URLs."""
from __future__ import annotations

import ipaddress
import socket
import urllib.parse
from contextlib import contextmanager
from pathlib import Path

import requests


class UnsafeUrlError(ValueError):
    pass


class DownloadTooLargeError(RuntimeError):
    pass


def validate_public_url(url: str) -> str:
    """Return a normalized HTTP(S) URL whose host resolves only publicly."""
    try:
        parsed = urllib.parse.urlsplit(str(url).strip())
    except ValueError as exc:
        raise UnsafeUrlError("Invalid URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeUrlError("Only public HTTP and HTTPS URLs are supported")
    if parsed.username or parsed.password:
        raise UnsafeUrlError("URLs containing credentials are not supported")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise UnsafeUrlError("Local network URLs are not supported")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise UnsafeUrlError("URL hostname could not be resolved") from exc
    if not addresses:
        raise UnsafeUrlError("URL hostname could not be resolved")
    for value in addresses:
        address = ipaddress.ip_address(value.split("%", 1)[0])
        if not address.is_global:
            raise UnsafeUrlError("Local or private network URLs are not supported")
    return urllib.parse.urlunsplit(parsed)


@contextmanager
def safe_response(url: str, *, timeout: int | float, headers: dict | None = None,
                  stream: bool = True, max_redirects: int = 5):
    """Open a public URL while validating every redirect target."""
    session = requests.Session()
    session.trust_env = False
    response = None
    current = url
    try:
        for _ in range(max_redirects + 1):
            current = validate_public_url(current)
            response = session.get(
                current, headers=headers, timeout=timeout, stream=stream,
                allow_redirects=False,
            )
            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get("Location", "")
                response.close()
                response = None
                if not location:
                    raise UnsafeUrlError("Redirect response omitted its destination")
                current = urllib.parse.urljoin(current, location)
                continue
            response.raise_for_status()
            yield response
            return
        raise UnsafeUrlError("Too many URL redirects")
    finally:
        if response is not None:
            response.close()
        session.close()


def _content_length(response) -> int | None:
    try:
        value = int(response.headers.get("Content-Length", ""))
        return value if value >= 0 else None
    except (TypeError, ValueError):
        return None


def safe_get_bytes(url: str, *, max_bytes: int, timeout: int | float,
                   headers: dict | None = None) -> bytes:
    with safe_response(url, timeout=timeout, headers=headers) as response:
        expected = _content_length(response)
        if expected is not None and expected > max_bytes:
            raise DownloadTooLargeError(f"Remote response exceeds {max_bytes} bytes")
        payload = bytearray()
        for chunk in response.iter_content(chunk_size=65_536):
            if not chunk:
                continue
            payload.extend(chunk)
            if len(payload) > max_bytes:
                raise DownloadTooLargeError(f"Remote response exceeds {max_bytes} bytes")
        return bytes(payload)


def safe_download(url: str, destination: Path, *, max_bytes: int,
                  timeout: int | float, headers: dict | None = None) -> int:
    """Stream a public URL to disk, aborting before it exceeds ``max_bytes``."""
    total = 0
    try:
        with safe_response(url, timeout=timeout, headers=headers) as response:
            expected = _content_length(response)
            if expected is not None and expected > max_bytes:
                raise DownloadTooLargeError(f"Remote audio exceeds {max_bytes} bytes")
            with open(destination, "wb") as handle:
                for chunk in response.iter_content(chunk_size=65_536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise DownloadTooLargeError(f"Remote audio exceeds {max_bytes} bytes")
                    handle.write(chunk)
        return total
    except Exception:
        destination.unlink(missing_ok=True)
        raise
