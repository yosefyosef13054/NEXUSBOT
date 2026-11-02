"""Outbound HTTP fetching with SSRF protection.

Used by URL ingestion and the `fetch_url` tool. Every hop of a redirect chain is checked:
the scheme must be http(s), the host must resolve only to public addresses (unless
`WEB_FETCH_ALLOW_PRIVATE` is on, for tests), and the body is capped in size.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from app.config import settings

_BLOCKED_NETWORKS = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
]

TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "text/markdown", "application/json")
BINARY_TYPES = ("application/pdf",)
MAX_REDIRECTS = 5


class UnsafeUrlError(ValueError):
    """The URL must not be fetched (scheme, private address, redirect chain, size)."""


@dataclass(slots=True)
class FetchedResource:
    url: str
    final_url: str
    content_type: str
    body: bytes
    title: str | None = None

    @property
    def is_html(self) -> bool:
        return self.content_type.startswith(("text/html", "application/xhtml+xml"))

    @property
    def is_pdf(self) -> bool:
        return self.content_type.startswith("application/pdf")


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not any(ip in net for net in _BLOCKED_NETWORKS)


def _resolve(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return sorted({info[4][0] for info in infos})


async def validate_public_url(url: str) -> str:
    """Return the normalized URL, raising `UnsafeUrlError` for anything we must not touch."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise UnsafeUrlError("Only http(s) URLs are allowed")
    if not parts.hostname:
        raise UnsafeUrlError("URL has no host")
    if parts.username or parts.password:
        raise UnsafeUrlError("Credentials in URLs are not allowed")
    host = parts.hostname
    if settings.WEB_FETCH_ALLOW_PRIVATE:
        return parts.geturl()
    if host.lower() in ("localhost",) or host.endswith(".localhost"):
        raise UnsafeUrlError("Private hosts are not allowed")
    try:
        addresses = await asyncio.to_thread(_resolve, host)
    except socket.gaierror as e:
        raise UnsafeUrlError(f"Could not resolve host '{host}'") from e
    if not addresses:
        raise UnsafeUrlError(f"Could not resolve host '{host}'")
    for addr in addresses:
        if not _is_public(ipaddress.ip_address(addr)):
            raise UnsafeUrlError("URL resolves to a private or reserved address")
    return parts.geturl()


def _content_type(headers: httpx.Headers) -> str:
    return (headers.get("content-type") or "application/octet-stream").split(";")[0].strip().lower()


def _title_from_html(body: bytes) -> str | None:
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(body[:200_000], "html.parser")
        if soup.title and soup.title.string:
            return " ".join(soup.title.string.split())[:200]
    except Exception:  # noqa: BLE001
        return None
    return None


async def fetch_url_safely(
    url: str,
    *,
    max_bytes: int | None = None,
    timeout: float | None = None,
    allowed_types: tuple[str, ...] = TEXT_TYPES + BINARY_TYPES,
) -> FetchedResource:
    """Download `url` following at most MAX_REDIRECTS redirects, validating each hop."""
    cap = max_bytes or settings.WEB_FETCH_MAX_BYTES
    current = await validate_public_url(url)
    headers = {"User-Agent": "NexusBot/1.2 (+https://github.com/yosefyosef13054/NEXUSBOT)", "Accept": "*/*"}
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=timeout or settings.WEB_FETCH_TIMEOUT_SECONDS,
        trust_env=settings.HTTP_TRUST_ENV,
        headers=headers,
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            async with client.stream("GET", current) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise UnsafeUrlError("Redirect without Location header")
                    current = await validate_public_url(urljoin(current, location))
                    continue
                if response.status_code >= 400:
                    raise UnsafeUrlError(f"Remote server answered {response.status_code}")
                ctype = _content_type(response.headers)
                if not ctype.startswith(allowed_types):
                    raise UnsafeUrlError(f"Unsupported content type '{ctype}'")
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > cap:
                    raise UnsafeUrlError(f"Resource larger than {cap} bytes")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > cap:
                        raise UnsafeUrlError(f"Resource larger than {cap} bytes")
                    chunks.append(chunk)
                body = b"".join(chunks)
                title = (
                    _title_from_html(body)
                    if ctype.startswith(("text/html", "application/xhtml+xml"))
                    else None
                )
                return FetchedResource(url=url, final_url=current, content_type=ctype, body=body, title=title)
    raise UnsafeUrlError("Too many redirects")


def html_to_text(body: bytes, *, max_chars: int | None = None) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(body, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    cleaned = "\n".join(line for line in lines if line)
    return cleaned[:max_chars] if max_chars else cleaned
