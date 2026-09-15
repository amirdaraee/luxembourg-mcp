import http.client
import io
import json
import socket
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request
from xml.etree import ElementTree

from luxembourg_mcp.http import HttpClient, UpstreamError, validate_external_url
from luxembourg_mcp.providers import MAX_GTFS_MEMBER_BYTES, LuxembourgData, _parse_xml, _read_bounded_zip_member
from luxembourg_mcp.server import MAX_REQUEST_BYTES, McpServer, RateLimiter, _origin_is_local, _rate_limit_key


class FakeHeaders(dict):
    def get_content_charset(self):
        return "utf-8"


class FakeResponse:
    def __init__(self, payload, headers=None):
        self.payload = payload
        self.headers = FakeHeaders(headers or {})

    def read(self, size=-1):
        return self.payload if size < 0 else self.payload[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    def __init__(self, response):
        self.response = response

    def open(self, request, timeout=None):
        return self.response


class FakeZipInfo:
    flag_bits = 0

    def __init__(self, file_size, compress_size):
        self.file_size = file_size
        self.compress_size = compress_size


class FakeZip:
    def __init__(self, info):
        self.info = info
        self.opened = False

    def getinfo(self, name):
        return self.info

    def open(self, info):
        self.opened = True
        raise AssertionError("oversized ZIP member must not be opened")


class SecurityTests(unittest.TestCase):
    def test_upstream_read_rejects_declared_oversize(self):
        response = FakeResponse(b"small", {"Content-Length": "100"})
        with patch("luxembourg_mcp.http.build_opener", return_value=FakeOpener(response)):
            with self.assertRaisesRegex(UpstreamError, "exceeds 8 bytes"):
                HttpClient().get_bytes("https://example.test/data", max_bytes=8)

    def test_upstream_read_rejects_streamed_oversize(self):
        response = FakeResponse(b"123456789")
        with patch("luxembourg_mcp.http.build_opener", return_value=FakeOpener(response)):
            with self.assertRaisesRegex(UpstreamError, "exceeds 8 bytes"):
                HttpClient().get_bytes("https://example.test/data", max_bytes=8)

    def test_hardcoded_urls_only_follow_same_host_https_redirects(self):
        handlers = []

        def fake_build_opener(handler):
            handlers.append(handler)
            return FakeOpener(FakeResponse(b"ok"))

        with patch("luxembourg_mcp.http.build_opener", side_effect=fake_build_opener):
            self.assertEqual(HttpClient().get_bytes("https://api.example.test/data")[0], b"ok")
        handler = handlers[0]
        request = Request("https://api.example.test/data")
        for target in ("https://evil.example/x", "http://api.example.test/x", "https://other.example.test/x", "https://127.0.0.1/x"):
            with self.subTest(target=target), self.assertRaises(UpstreamError):
                handler.redirect_request(request, None, 302, "Found", {}, target)
        moved = handler.redirect_request(request, None, 302, "Found", {}, "https://api.example.test/moved")
        self.assertEqual(moved.full_url, "https://api.example.test/moved")

    def test_hardcoded_urls_must_be_https(self):
        with patch("luxembourg_mcp.http.build_opener", return_value=FakeOpener(FakeResponse(b"ok"))):
            with self.assertRaisesRegex(UpstreamError, "trusted HTTPS"):
                HttpClient().get_bytes("http://api.example.test/data")

    def test_xml_entity_declarations_are_rejected(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;">]><r>&b;</r>'
        for payload in (bomb, bomb.decode().encode("utf-16"), b'<!DOCTYPE r [<!ENTITY % p "x">]><r/>'):
            with self.subTest(payload=payload[:20]), self.assertRaises(ElementTree.ParseError):
                _parse_xml(payload)
        self.assertEqual(_parse_xml(b'<!DOCTYPE r><r><c>1</c></r>').findtext("c"), "1")
        self.assertEqual(_parse_xml(b"<r>&amp;</r>").text, "&")

    def test_provider_xml_with_entities_is_an_upstream_error(self):
        class XmlHttp:
            def get_bytes(self, url, headers=None, **kwargs):
                return b'<!DOCTYPE r [<!ENTITY a "x">]><r>&a;</r>', "utf-8"

        with self.assertRaises(UpstreamError):
            LuxembourgData(XmlHttp()).get_traffic("a6")

    def test_user_values_cannot_be_url_dot_segments(self):
        class NoHttp:
            def get_json(self, url, **kwargs):
                raise AssertionError(f"must not fetch {url}")

            def get_bytes(self, url, headers=None, **kwargs):
                raise AssertionError(f"must not fetch {url}")

        data = LuxembourgData(NoHttp())
        cases = [
            lambda: data.get_dataset(".."),
            lambda: data.get_dataset("."),
            lambda: data.get_statistics("DF_D7100", key=".."),
            lambda: data.get_statistics("DF_D7100", key="."),
            lambda: data.get_geo_features("addresses/./x"),
            lambda: data.get_geo_features(".."),
        ]
        for index, call in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(ValueError):
                call()

    def test_sdmx_wildcard_keys_with_dots_are_still_allowed(self):
        fetched = []

        class CsvHttp:
            def get_bytes(self, url, headers=None, **kwargs):
                fetched.append(url)
                return b"A,B\n1,2\n", "utf-8"

        LuxembourgData(CsvHttp()).get_statistics("DF_D7100", key="A..B")
        self.assertIn("/A..B?", fetched[0])

    def test_indirect_urls_require_allowlisted_https_host(self):
        allowed = frozenset({"download.data.public.lu", "169.254.169.254"})
        validate_external_url("https://download.data.public.lu/resource.csv", allowed)
        rejected = [
            "http://download.data.public.lu/resource.csv",
            "https://evil.example/resource.csv",
            "https://user:pass@download.data.public.lu/resource.csv",
            "https://169.254.169.254/latest/meta-data",
        ]
        for url in rejected:
            with self.subTest(url=url), self.assertRaises(UpstreamError):
                validate_external_url(url, allowed)

    def test_gtfs_member_size_is_checked_before_decompression(self):
        archive = FakeZip(FakeZipInfo(MAX_GTFS_MEMBER_BYTES + 1, 1000))
        with self.assertRaisesRegex(UpstreamError, "exceeds"):
            _read_bounded_zip_member(archive, "stops.txt")
        self.assertFalse(archive.opened)

    def test_gtfs_suspicious_compression_ratio_is_rejected(self):
        archive = FakeZip(FakeZipInfo(2 * 1024 * 1024, 1000))
        with self.assertRaisesRegex(UpstreamError, "suspicious compression ratio"):
            _read_bounded_zip_member(archive, "stops.txt")
        self.assertFalse(archive.opened)

    def test_gtfs_normal_member_is_read(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("stops.txt", "stop_id,stop_name\n1,Hamilius\n")
        with zipfile.ZipFile(io.BytesIO(payload.getvalue())) as archive:
            self.assertIn(b"Hamilius", _read_bounded_zip_member(archive, "stops.txt"))

    def test_rate_limiter_enforces_window(self):
        limiter = RateLimiter(2, window_seconds=60)
        self.assertTrue(limiter.allow("client", now=0))
        self.assertTrue(limiter.allow("client", now=1))
        self.assertFalse(limiter.allow("client", now=2))
        self.assertTrue(limiter.allow("client", now=61))

    def test_http_rejects_oversized_content_length_without_reading_body(self):
        server = McpServer().create_http_server("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            connection.putrequest("POST", "/mcp")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(MAX_REQUEST_BYTES + 1))
            connection.endheaders()
            response = connection.getresponse()
            body = json.loads(response.read())
            self.assertEqual(response.status, 413)
            self.assertIn("exceeds", body["error"])
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_http_rate_limit_returns_429(self):
        with patch.dict("os.environ", {"LUXEMBOURG_MCP_RATE_LIMIT": "1"}):
            server = McpServer().create_http_server("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        body = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
        try:
            statuses = []
            for _ in range(2):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
                connection.request("POST", "/mcp", body=body, headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                statuses.append(response.status)
                response.read()
                connection.close()
            self.assertEqual(statuses, [200, 429])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_origin_check_requires_exact_local_hostname(self):
        for origin in ("http://localhost", "http://localhost:3000", "https://localhost", "http://127.0.0.1:8000", "http://[::1]:8000"):
            with self.subTest(origin=origin):
                self.assertTrue(_origin_is_local(origin))
        for origin in (
            "http://localhost.evil.example",
            "http://127.0.0.1.evil.example",
            "http://[::1].evil.example",
            "http://localhost@evil.example",
            "https://evil.example",
            "file://localhost/etc",
            "null",
            "",
        ):
            with self.subTest(origin=origin):
                self.assertFalse(_origin_is_local(origin))

    def test_allowed_origins_env_permits_configured_origin(self):
        body = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
        with patch.dict("os.environ", {"LUXEMBOURG_MCP_ALLOWED_ORIGINS": "https://luxembourg-mcp.com"}):
            server = McpServer().create_http_server("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            statuses = {}
            for origin in ("https://luxembourg-mcp.com", "https://evil.example"):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
                connection.request("POST", "/mcp", body=body, headers={"Content-Type": "application/json", "Origin": origin})
                response = connection.getresponse()
                statuses[origin] = response.status
                response.read()
                connection.close()
            self.assertEqual(statuses["https://luxembourg-mcp.com"], 200)
            self.assertEqual(statuses["https://evil.example"], 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_client_ip_header_buckets_rate_limit_per_forwarded_ip(self):
        body = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
        env = {"LUXEMBOURG_MCP_RATE_LIMIT": "1", "LUXEMBOURG_MCP_CLIENT_IP_HEADER": "CF-Connecting-IP"}
        with patch.dict("os.environ", env):
            server = McpServer().create_http_server("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            statuses = []
            for forwarded in ("198.51.100.1", "198.51.100.2", "198.51.100.1"):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
                connection.request("POST", "/mcp", body=body, headers={"Content-Type": "application/json", "CF-Connecting-IP": forwarded})
                response = connection.getresponse()
                statuses.append(response.status)
                response.read()
                connection.close()
            self.assertEqual(statuses, [200, 200, 429])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_cors_preflight_and_response_headers_for_allowed_origin(self):
        body = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
        with patch.dict("os.environ", {"LUXEMBOURG_MCP_ALLOWED_ORIGINS": "*"}):
            server = McpServer().create_http_server("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            connection.request("OPTIONS", "/mcp", headers={"Origin": "https://claude.ai"})
            preflight = connection.getresponse()
            preflight.read()
            self.assertEqual(preflight.status, 204)
            self.assertEqual(preflight.getheader("Access-Control-Allow-Origin"), "https://claude.ai")
            self.assertIn("POST", preflight.getheader("Access-Control-Allow-Methods"))
            connection.close()

            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            connection.request("POST", "/mcp", body=body, headers={"Content-Type": "application/json", "Origin": "https://claude.ai"})
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Access-Control-Allow-Origin"), "https://claude.ai")
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_docker_runs_as_non_root_user(self):
        dockerfile = Path("Dockerfile").read_text()
        self.assertIn("USER app", dockerfile)

    def test_rate_limit_key_groups_ipv6_by_64_prefix(self):
        self.assertEqual(_rate_limit_key("2001:db8:1:2::1"), _rate_limit_key("2001:db8:1:2:ffff::9"))
        self.assertNotEqual(_rate_limit_key("2001:db8:1:2::1"), _rate_limit_key("2001:db8:1:3::1"))
        self.assertEqual(_rate_limit_key("198.51.100.7"), "198.51.100.7")
        self.assertEqual(_rate_limit_key("not-an-ip"), "not-an-ip")

    def test_http_rate_limit_cannot_be_escaped_by_rotating_ipv6_addresses(self):
        body = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
        env = {"LUXEMBOURG_MCP_RATE_LIMIT": "1", "LUXEMBOURG_MCP_CLIENT_IP_HEADER": "CF-Connecting-IP"}
        with patch.dict("os.environ", env):
            server = McpServer().create_http_server("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            statuses = []
            for forwarded in ("2001:db8:1:2::1", "2001:db8:1:2::2"):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
                connection.request("POST", "/mcp", body=body, headers={"Content-Type": "application/json", "CF-Connecting-IP": forwarded})
                response = connection.getresponse()
                statuses.append(response.status)
                response.read()
                connection.close()
            self.assertEqual(statuses, [200, 429])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_http_deeply_nested_json_is_a_parse_error(self):
        server = McpServer().create_http_server("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for payload in (b"[" * 100_000, b'{"id":' + b"9" * 5000 + b"}", b"\xff\xfe"):
                with self.subTest(payload=payload[:10]):
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
                    connection.request("POST", "/mcp", body=payload, headers={"Content-Type": "application/json"})
                    response = connection.getresponse()
                    body = json.loads(response.read())
                    connection.close()
                    self.assertEqual(response.status, 400)
                    self.assertEqual(body["error"]["code"], -32700)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_http_idle_connection_is_closed_after_timeout(self):
        with patch("luxembourg_mcp.server.REQUEST_TIMEOUT_SECONDS", 0.3):
            server = McpServer().create_http_server("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with socket.create_connection(server.server_address, timeout=5) as idle:
                idle.sendall(b"POST /mcp HTTP/1.0\r\nContent-Length: 100\r\n\r\n{")
                self.assertEqual(idle.recv(1024), b"")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_http_connections_beyond_cap_are_dropped_and_slots_recover(self):
        with patch.dict("os.environ", {"LUXEMBOURG_MCP_MAX_CONNECTIONS": "2"}):
            server = McpServer().create_http_server("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        body = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
        try:
            holders = [socket.create_connection(server.server_address, timeout=5) for _ in range(2)]
            for holder in holders:
                holder.sendall(b"POST /mcp HTTP/1.0\r\n")  # headers never finish
            time.sleep(0.2)
            with socket.create_connection(server.server_address, timeout=5) as rejected:
                self.assertEqual(rejected.recv(1024), b"")
            for holder in holders:
                holder.close()
            time.sleep(0.2)
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            connection.request("POST", "/mcp", body=body, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            response.read()
            connection.close()
            self.assertEqual(response.status, 200)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_http_access_log_escapes_control_characters(self):
        server = McpServer().create_http_server("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        stderr = io.StringIO()
        try:
            with patch("sys.stderr", stderr):
                with socket.create_connection(server.server_address, timeout=5) as raw:
                    raw.sendall(b"GET /\x1b[2J\x1b[31mFORGED\rline HTTP/1.0\r\n\r\n")
                    while raw.recv(4096):
                        pass
                time.sleep(0.1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        logged = stderr.getvalue()
        self.assertIn("FORGED", logged)
        self.assertNotIn("\x1b", logged)
        self.assertNotIn("\r", logged)
        self.assertIn("\\x1b", logged)

    def test_tools_call_with_unhashable_name_is_invalid_params(self):
        response = McpServer().handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": [1]}})
        self.assertEqual(response["error"]["code"], -32602)

    def test_stdio_survives_malformed_lines(self):
        lines = [
            '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":[1]}}',
            "[" * 100_000,
            '{"jsonrpc":"2.0","id":3,"method":"ping"}',
        ]
        stdout = io.StringIO()
        with patch("sys.stdin", io.StringIO("\n".join(lines) + "\n")), patch("sys.stdout", stdout):
            McpServer().run_stdio()
        responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual([r.get("error", {}).get("code") for r in responses], [-32602, -32700, None])
        self.assertEqual(responses[-1]["result"], {})

    def test_cache_runs_one_loader_per_key_under_concurrency(self):
        data = LuxembourgData(HttpClient())
        calls = []
        start = threading.Barrier(20)

        def loader():
            calls.append(1)
            time.sleep(0.1)
            return "payload"

        def worker(results):
            start.wait()
            results.append(data._cached("gtfs:x", 60, loader))

        results = []
        workers = [threading.Thread(target=worker, args=(results,)) for _ in range(20)]
        for item in workers:
            item.start()
        for item in workers:
            item.join(timeout=5)
        self.assertEqual(len(calls), 1)
        self.assertEqual(results, ["payload"] * 20)
        self.assertEqual(data._cache_key_locks, {})


if __name__ == "__main__":
    unittest.main()
