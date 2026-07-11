"""Small JSON/text HTTP client with consistent upstream errors."""

from __future__ import annotations

import json
import ipaddress
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

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
    ) -> tuple[bytes, str]:
        if allowed_hosts is not None:
            validate_external_url(url, allowed_hosts)
        request_headers = {
            "Accept": "application/json, text/csv;q=0.9, application/xml;q=0.8",
            "User-Agent": "luxembourg-mcp/0.4",
        }
        request_headers.update(headers or {})
        request = Request(
            url,
            headers=request_headers,
        )
        try:
            opener = build_opener(_SafeRedirectHandler(allowed_hosts)) if allowed_hosts is not None else None
            response_context = opener.open(request, timeout=20) if opener is not None else urlopen(request, timeout=20)
            with response_context as response:
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

    def get_json_value(
        self,
        url: str,
        *,
        max_bytes: int = MAX_UPSTREAM_BYTES,
        allowed_hosts: set[str] | frozenset[str] | None = None,
    ) -> Any:
        payload, charset = self.get_bytes(url, max_bytes=max_bytes, allowed_hosts=allowed_hosts)
        try:
            return json.loads(payload.decode(charset))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpstreamError(f"Upstream returned invalid JSON for {url}") from exc

    def get_json(
        self,
        url: str,
        *,
        max_bytes: int = MAX_UPSTREAM_BYTES,
        allowed_hosts: set[str] | frozenset[str] | None = None,
    ) -> dict:
        value = self.get_json_value(url, max_bytes=max_bytes, allowed_hosts=allowed_hosts)
        if not isinstance(value, dict):
            raise UpstreamError(f"Upstream returned an unexpected JSON shape for {url}")
        return value
