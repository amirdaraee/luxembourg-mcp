"""Minimal MCP 2025-11-25 server for stdio and stateless HTTP."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any, Callable
from urllib.parse import urlsplit

from .http import UpstreamError
from .providers import LuxembourgData

PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
MAX_REQUEST_BYTES = 1024 * 1024
DEFAULT_RATE_LIMIT = 60


def catalog_html() -> bytes:
    return files("luxembourg_mcp").joinpath("static/index.html").read_bytes()


def _origin_is_local(origin: str) -> bool:
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and (parsed.hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict
    function: Callable[..., dict]

    def definition(self) -> dict:
        return {"name": self.name, "description": self.description, "inputSchema": self.schema}


def _object_schema(properties: dict, required: list[str] | None = None) -> dict:
    value = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        value["required"] = required
    return value


def _json_type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def validate_schema(value: Any, schema: dict, path: str = "arguments") -> None:
    expected = schema.get("type")
    if expected and not _json_type_matches(value, expected):
        raise ValueError(f"{path} must be {expected}")
    if "enum" in schema and value not in schema["enum"]:
        choices = ", ".join(str(item) for item in schema["enum"])
        raise ValueError(f"{path} must be one of: {choices}")
    if expected == "object":
        properties = schema.get("properties", {})
        missing = [name for name in schema.get("required", []) if name not in value]
        if missing:
            raise ValueError(f"{path} is missing required field: {missing[0]}")
        if schema.get("additionalProperties") is False:
            unexpected = [name for name in value if name not in properties]
            if unexpected:
                raise ValueError(f"{path} has unexpected field: {unexpected[0]}")
        for name, item in value.items():
            if name in properties:
                validate_schema(item, properties[name], f"{path}.{name}")
    if expected == "array":
        if len(value) < schema.get("minItems", 0):
            raise ValueError(f"{path} has too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ValueError(f"{path} has too many items")
        if "items" in schema:
            for index, item in enumerate(value):
                validate_schema(item, schema["items"], f"{path}[{index}]")
    if expected in {"integer", "number"}:
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{path} must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{path} must be at most {schema['maximum']}")


class RateLimiter:
    def __init__(self, limit: int, window_seconds: int = 60, max_clients: int = 4096):
        self.limit = max(limit, 0)
        self.window_seconds = window_seconds
        self.max_clients = max_clients
        self._requests: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, client: str, now: float | None = None) -> bool:
        if self.limit == 0:
            return True
        current = time.monotonic() if now is None else now
        cutoff = current - self.window_seconds
        with self._lock:
            bucket = self._requests.get(client)
            if bucket is None:
                if len(self._requests) >= self.max_clients:
                    stale = [name for name, values in self._requests.items() if not values or values[-1] <= cutoff]
                    for name in stale:
                        self._requests.pop(name, None)
                    if len(self._requests) >= self.max_clients:
                        oldest = min(self._requests, key=lambda name: self._requests[name][-1])
                        self._requests.pop(oldest)
                bucket = deque()
                self._requests[client] = bucket
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False
            bucket.append(current)
            return True


class McpServer:
    def __init__(self, data: LuxembourgData | None = None):
        source = data or LuxembourgData()
        self.tools = {
            tool.name: tool for tool in [
                Tool("search_datasets", "Search Luxembourg's official data.public.lu catalog.", _object_schema({"query": {"type": "string"}, "page": {"type": "integer", "minimum": 1, "default": 1}, "page_size": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}}, ["query"]), source.search_datasets),
                Tool("get_dataset", "Get metadata and current download resources for an official dataset.", _object_schema({"dataset_id_or_slug": {"type": "string"}}, ["dataset_id_or_slug"]), source.get_dataset),
                Tool("geocode_address", "Resolve a Luxembourg address to official Geoportail coordinates.", _object_schema({"query": {"type": "string"}}, ["query"]), source.geocode_address),
                Tool("reverse_geocode", "Find the nearest official Luxembourg address to WGS84 coordinates.", _object_schema({"latitude": {"type": "number"}, "longitude": {"type": "number"}}, ["latitude", "longitude"]), source.reverse_geocode),
                Tool("list_geo_collections", "Search official Geoportail OGC feature collections by title, description, or keyword.", _object_schema({"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}}), source.list_geo_collections),
                Tool("get_geo_features", "Fetch GeoJSON features from an official Geoportail OGC collection.", _object_schema({"collection_id": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10}, "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4, "description": "WGS84 [west, south, east, north]"}}, ["collection_id"]), source.get_geo_features),
                Tool("get_weather_alerts", "Get current official MeteoLux weather warnings published on data.public.lu.", _object_schema({"language": {"type": "string", "enum": ["en", "fr", "de", "lu"], "default": "en"}}), source.get_weather_alerts),
                Tool("search_legislation", "Search official Luxembourg legislation and consolidated laws through Legilux.", _object_schema({"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}}, ["query"]), source.search_legislation),
                Tool("search_statistics", "Search STATEC LUSTAT statistical dataflows by topic or title.", _object_schema({"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}}, ["query"]), source.search_statistics),
                Tool("get_statistics", "Retrieve recent observations from a STATEC LUSTAT SDMX dataflow.", _object_schema({"dataflow_id": {"type": "string", "description": "STATEC identifier such as DF_D7100"}, "key": {"type": "string", "default": "all", "description": "SDMX dimension key or all"}, "last_n_observations": {"type": "integer", "minimum": 1, "maximum": 100, "default": 5}, "max_rows": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 500}}, ["dataflow_id"]), source.get_statistics),
                Tool("get_city_parking", "Get current Ville de Luxembourg car-park capacity and free spaces.", _object_schema({"query": {"type": "string"}, "available_only": {"type": "boolean", "default": False}}), source.get_city_parking),
                Tool("list_cfl_parking", "List official CFL Park and Ride facilities.", _object_schema({}), source.list_cfl_parking),
                Tool("get_cfl_parking", "Get live occupancy and details for a CFL Park and Ride facility.", _object_schema({"parking_id": {"type": "string"}}, ["parking_id"]), source.get_cfl_parking),
                Tool("get_traffic", "Get live CITA motorway speed, occupancy, and flow measurements.", _object_schema({"road": {"type": "string", "enum": ["a3", "a4", "a6", "a7", "a13", "b40"], "default": "a6"}}), source.get_traffic),
                Tool("get_water_levels", "Get the latest official measured water level at Luxembourg stations.", _object_schema({"station": {"type": "string", "description": "Optional station-name filter"}}), source.get_water_levels),
                Tool("get_air_quality", "Get the latest national telemetric air-quality measurements by station.", _object_schema({"city": {"type": "string", "description": "Optional city-name filter"}}), source.get_air_quality),
                Tool("search_chamber_bodies", "Search official Chamber committees, delegations, bodies, memberships, and roles.", _object_schema({"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}}, ["query"]), source.search_chamber_bodies),
                Tool("get_accessibility_figures", "Get national Digital Accessibility Observatory key figures.", _object_schema({}), source.get_accessibility_figures),
                Tool("get_accessibility_audits", "Get the latest public-sector digital accessibility audits.", _object_schema({"limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10}}), source.get_accessibility_audits),
                Tool("search_transit_stops", "Search official nationwide public-transport stops from the current ATP GTFS feed.", _object_schema({"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}}, ["query"]), source.search_transit_stops),
                Tool("get_city_mobility", "Get Ville de Luxembourg mobility locations as GeoJSON features.", _object_schema({"category": {"type": "string", "enum": ["park_and_bike", "park_and_ride", "covered_parking", "surface_parking", "accessible_parking", "bike_rentals"]}}, ["category"]), source.get_city_mobility),
                Tool("get_weather_observations", "Get live MeteoLux weather observations at Luxembourg-Airport: temperature, wind, pressure, humidity, visibility.", _object_schema({}), source.get_weather_observations),
                Tool("get_public_holidays", "List Luxembourg legal public holidays in four languages, optionally for one year.", _object_schema({"year": {"type": "integer", "minimum": 2020, "maximum": 2100}}), source.get_public_holidays),
                Tool("search_parliamentary_questions", "Search Chamber of Deputies parliamentary questions by keyword, newest first.", _object_schema({"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}}, ["query"]), source.search_parliamentary_questions),
                Tool("get_housing_prices", "Get advertised housing sale prices by commune from the official Observatoire de l'Habitat.", _object_schema({"property_type": {"type": "string", "enum": ["apartment", "house"], "default": "apartment"}, "commune": {"type": "string", "description": "Optional commune-name filter"}, "year": {"type": "string", "description": "Four-digit year such as 2025; defaults to the latest"}}), source.get_housing_prices),
                Tool("get_election_results", "Get machine-readable 2023 legislative election results, national and per circonscription.", _object_schema({}), source.get_election_results),
                Tool("get_ev_charging", "Get Chargy public EV charging stations with live connector availability.", _object_schema({"query": {"type": "string", "description": "Optional name or address filter"}, "available_only": {"type": "boolean", "default": False}}), source.get_ev_charging),
            ]
        }

    @staticmethod
    def _result(request_id: Any, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def handle(self, request: Any) -> dict | None:
        if not isinstance(request, dict):
            return self._error(None, -32600, "Invalid Request: expected a JSON object")
        request_id = request.get("id")
        if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return self._error(request_id, -32600, "Invalid Request")
        if "id" in request and (
            isinstance(request_id, bool) or not isinstance(request_id, (str, int, float, type(None)))
        ):
            return self._error(None, -32600, "Invalid Request: invalid id")
        params = request.get("params", {})
        if not isinstance(params, dict):
            return self._error(request_id, -32602, "Invalid params: expected an object")
        if "id" not in request:
            return None
        method = request.get("method")
        if method == "initialize":
            requested_version = params.get("protocolVersion")
            if not isinstance(requested_version, str):
                return self._error(request_id, -32602, "Invalid params: protocolVersion is required")
            negotiated_version = requested_version if requested_version in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
            return self._result(request_id, {
                "protocolVersion": negotiated_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "luxembourg-mcp", "version": "0.4.1"},
                "instructions": "Keyless access to official Luxembourg public data through 27 tools. Results include upstream source URLs.",
            })
        if method == "ping":
            return self._result(request_id, {})
        if method == "tools/list":
            return self._result(request_id, {"tools": [tool.definition() for tool in self.tools.values()]})
        if method == "tools/call":
            tool = self.tools.get(params.get("name"))
            if tool is None:
                return self._error(request_id, -32602, f"Unknown tool: {params.get('name')}")
            try:
                arguments = params.get("arguments", {})
                validate_schema(arguments, tool.schema)
                value = tool.function(**arguments)
                return self._result(request_id, {
                    "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, indent=2)}],
                    "structuredContent": value,
                    "isError": False,
                })
            except (TypeError, ValueError) as exc:
                message = f"Invalid arguments: {exc}"
            except UpstreamError as exc:
                message = str(exc)
            except Exception as exc:
                message = f"Tool failed: {exc}"
            return self._result(request_id, {"content": [{"type": "text", "text": message}], "isError": True})
        return self._error(request_id, -32601, f"Method not found: {method}")

    def run_stdio(self) -> None:
        for line in sys.stdin:
            try:
                request = json.loads(line)
                response = self.handle(request)
            except json.JSONDecodeError:
                response = self._error(None, -32700, "Parse error")
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()

    def create_http_server(self, host: str, port: int) -> ThreadingHTTPServer:
        mcp = self
        try:
            rate_limit = int(os.environ.get("LUXEMBOURG_MCP_RATE_LIMIT", str(DEFAULT_RATE_LIMIT)))
        except ValueError:
            rate_limit = DEFAULT_RATE_LIMIT
        limiter = RateLimiter(rate_limit)
        allowed_origins = frozenset(
            item.strip() for item in os.environ.get("LUXEMBOURG_MCP_ALLOWED_ORIGINS", "").split(",") if item.strip()
        )
        # Only honor a client-IP header when explicitly configured (i.e. behind a trusted proxy);
        # otherwise clients could spoof it to escape the rate limit.
        client_ip_header = os.environ.get("LUXEMBOURG_MCP_CLIENT_IP_HEADER") or None

        class Handler(BaseHTTPRequestHandler):
            def _origin_allowed(self, origin: str) -> bool:
                return _origin_is_local(origin) or origin in allowed_origins or "*" in allowed_origins

            def _cors_headers(self) -> dict[str, str]:
                origin = self.headers.get("Origin")
                if origin and self._origin_allowed(origin):
                    return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
                return {}

            def do_OPTIONS(self) -> None:
                self.send_response(204)
                for name, value in self._cors_headers().items():
                    self.send_header(name, value)
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, MCP-Protocol-Version, Mcp-Session-Id")
                self.send_header("Access-Control-Max-Age", "86400")
                self.end_headers()

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                if path == "/":
                    self._send_html(200, catalog_html())
                elif path == "/health":
                    self._send(200, {"status": "ok", "server": "luxembourg-mcp"})
                else:
                    self._send(405, {"error": "This stateless server does not offer an SSE stream"}, {"Allow": "POST"})

            def do_POST(self) -> None:
                if self.path.rstrip("/") != "/mcp":
                    self._send(404, {"error": "Not found"})
                    return
                origin = self.headers.get("Origin")
                if origin and not self._origin_allowed(origin):
                    self._send(403, {"error": "Origin is not allowed by this server"})
                    return
                client = (client_ip_header and self.headers.get(client_ip_header)) or self.client_address[0]
                if not limiter.allow(client):
                    self._send(429, {"error": "Rate limit exceeded"}, {"Retry-After": "60"})
                    return
                raw_length = self.headers.get("Content-Length")
                if raw_length is None:
                    self._send(411, {"error": "Content-Length is required"})
                    return
                try:
                    length = int(raw_length)
                    if length < 0:
                        raise ValueError
                except ValueError:
                    self._send(400, {"error": "Invalid Content-Length"})
                    return
                if length > MAX_REQUEST_BYTES:
                    self.close_connection = True
                    self._send(413, {"error": f"Request body exceeds {MAX_REQUEST_BYTES} bytes"}, {"Connection": "close"})
                    return
                try:
                    request = json.loads(self.rfile.read(length))
                except json.JSONDecodeError:
                    response = mcp._error(None, -32700, "Parse error")
                    self._send(400, response)
                    return
                protocol_version = self.headers.get("MCP-Protocol-Version") or "2025-03-26"
                if protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
                    response = mcp._error(request.get("id") if isinstance(request, dict) else None, -32602, "Unsupported protocol version")
                    self._send(400, response)
                    return
                response = mcp.handle(request)
                if response is None:
                    self.send_response(202)
                    self.end_headers()
                else:
                    self._send(200, response)

            def _send(self, status: int, body: dict, headers: dict[str, str] | None = None) -> None:
                encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                for name, value in {**self._cors_headers(), **(headers or {})}.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(encoded)

            def _send_html(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=300")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args: Any) -> None:
                sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")

        return ThreadingHTTPServer((host, port), Handler)

    def run_http(self, host: str, port: int) -> None:
        server = self.create_http_server(host, port)
        print(f"luxembourg-mcp listening on http://{host}:{port}/mcp", file=sys.stderr)
        server.serve_forever()
