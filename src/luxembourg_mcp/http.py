"""Small JSON/text HTTP client with consistent upstream errors."""

from __future__ import annotations

import json
import ipaddress
import os
import ssl
from functools import lru_cache
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

MAX_UPSTREAM_BYTES = 25 * 1024 * 1024


class UpstreamError(RuntimeError):
    pass


def validate_external_url(url: str, allowed_hosts: set[str] | frozenset[str]) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise UpstreamError("Upstream resource URL is invalid") from exc
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not hostname or parsed.username or parsed.password:
        raise UpstreamError("Upstream resource URL must use trusted HTTPS")
    if port not in (None, 443) or hostname not in allowed_hosts:
        raise UpstreamError(f"Upstream resource host is not trusted: {hostname or 'missing'}")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if not address.is_global:
        raise UpstreamError("Private or non-global upstream addresses are not allowed")


# Extra CA to trust on top of the system store. The hosted deployment sets it to the
# Cloudflare containers CA, because the Worker proxies a few upstreams (see
# deploy/cloudflare/src/index.js) and re-signs those TLS connections.
EXTRA_CA_ENV = "LUXEMBOURG_MCP_EXTRA_CA_FILE"


@lru_cache(maxsize=1)
def _tls_context() -> ssl.SSLContext | None:
    """System trust plus the extra CA, or None (urllib's default) when none is configured."""
    extra = os.environ.get(EXTRA_CA_ENV)
    if not extra or not os.path.isfile(extra):
        return None
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=extra)
    return context


def _opener(allowed_hosts: set[str] | frozenset[str]):
    context = _tls_context()
    redirects = _SafeRedirectHandler(allowed_hosts)
    return build_opener(redirects) if context is None else build_opener(HTTPSHandler(context=context), redirects)


class _SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self, allowed_hosts: set[str] | frozenset[str]):
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_external_url(newurl, self.allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpClient:
    def get_bytes(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        max_bytes: int = MAX_UPSTREAM_BYTES,
        allowed_hosts: set[str] | frozenset[str] | None = None,
        data: bytes | None = None,
    ) -> tuple[bytes, str]:
        if allowed_hosts is None:
            # Hardcoded upstream URLs: redirects may only stay on the same HTTPS host.
            allowed_hosts = frozenset({(urlsplit(url).hostname or "").lower()})
        validate_external_url(url, allowed_hosts)
        request_headers = {
            "Accept": "application/json, text/csv;q=0.9, application/xml;q=0.8",
            "User-Agent": "luxembourg-mcp/0.8",
        }
        request_headers.update(headers or {})
        request = Request(
            url,
            data=data,
            headers=request_headers,
        )
        try:
            opener = _opener(allowed_hosts)
            with opener.open(request, timeout=20) as response:
                declared_size = response.headers.get("Content-Length")
                if declared_size is not None:
                    try:
                        if int(declared_size) > max_bytes:
                            raise UpstreamError(f"Upstream response exceeds {max_bytes} bytes")
                    except ValueError:
                        pass
                payload = response.read(max_bytes + 1)
                if len(payload) > max_bytes:
                    raise UpstreamError(f"Upstream response exceeds {max_bytes} bytes")
                return payload, response.headers.get_content_charset() or "utf-8"
        except HTTPError as exc:
            raise UpstreamError(f"Upstream returned HTTP {exc.code} for {url}") from exc
        except (URLError, TimeoutError) as exc:
            raise UpstreamError(f"Could not reach upstream {url}: {exc}") from exc

    def probe(self, url: str, headers: dict[str, str] | None = None, *, timeout: float = 10, read_bytes: int = 4096,
              data: bytes | None = None) -> int:
        """Reachability check for a hardcoded URL: the HTTP status, reading only the start of the body."""
        allowed_hosts = frozenset({(urlsplit(url).hostname or "").lower()})
        validate_external_url(url, allowed_hosts)
        request = Request(url, data=data, headers={"Accept": "application/json, text/csv;q=0.9, application/xml;q=0.8",
                                        "User-Agent": "luxembourg-mcp/0.8 (status check)", **(headers or {})})
        try:
            with _opener(allowed_hosts).open(request, timeout=timeout) as response:
                response.read(read_bytes)
                return response.status
        except HTTPError as exc:
            raise UpstreamError(f"HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError, HTTPException) as exc:
            raise UpstreamError(f"unreachable: {getattr(exc, 'reason', exc)}") from exc

    def get_json_value(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        max_bytes: int = MAX_UPSTREAM_BYTES,
        allowed_hosts: set[str] | frozenset[str] | None = None,
    ) -> Any:
        payload, charset = self.get_bytes(url, headers, max_bytes=max_bytes, allowed_hosts=allowed_hosts)
        try:
            return json.loads(payload.decode(charset))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpstreamError(f"Upstream returned invalid JSON for {url}") from exc

    def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        max_bytes: int = MAX_UPSTREAM_BYTES,
        allowed_hosts: set[str] | frozenset[str] | None = None,
    ) -> dict:
        value = self.get_json_value(url, headers=headers, max_bytes=max_bytes, allowed_hosts=allowed_hosts)
        if not isinstance(value, dict):
            raise UpstreamError(f"Upstream returned an unexpected JSON shape for {url}")
        return value

    def post_json(
        self,
        url: str,
        body: Any,
        *,
        headers: dict[str, str] | None = None,
        max_bytes: int = MAX_UPSTREAM_BYTES,
    ) -> dict:
        """POST a JSON body to a hardcoded upstream URL and return its JSON object."""
        request_headers = {"Content-Type": "application/json", "Accept": "application/json", **(headers or {})}
        payload, charset = self.get_bytes(url, request_headers, max_bytes=max_bytes,
                                          data=json.dumps(body).encode("utf-8"))
        try:
            value = json.loads(payload.decode(charset))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpstreamError(f"Upstream returned invalid JSON for {url}") from exc
        if not isinstance(value, dict):
            raise UpstreamError(f"Upstream returned an unexpected JSON shape for {url}")
        return value
