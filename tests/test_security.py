import http.client
import io
import json
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from luxembourg_mcp.http import HttpClient, UpstreamError, validate_external_url
from luxembourg_mcp.providers import MAX_GTFS_MEMBER_BYTES, _read_bounded_zip_member
from luxembourg_mcp.server import MAX_REQUEST_BYTES, McpServer, RateLimiter, _origin_is_local


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
        with patch("luxembourg_mcp.http.urlopen", return_value=response):
            with self.assertRaisesRegex(UpstreamError, "exceeds 8 bytes"):
                HttpClient().get_bytes("https://example.test/data", max_bytes=8)

    def test_upstream_read_rejects_streamed_oversize(self):
        response = FakeResponse(b"123456789")
        with patch("luxembourg_mcp.http.urlopen", return_value=response):
            with self.assertRaisesRegex(UpstreamError, "exceeds 8 bytes"):
                HttpClient().get_bytes("https://example.test/data", max_bytes=8)

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

    def test_docker_runs_as_non_root_user(self):
        dockerfile = Path("Dockerfile").read_text()
        self.assertIn("USER app", dockerfile)


if __name__ == "__main__":
    unittest.main()
