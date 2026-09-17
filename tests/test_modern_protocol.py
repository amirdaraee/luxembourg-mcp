"""MCP 2026-07-28 (stateless, per-request `_meta`) alongside the legacy handshake era."""

import base64
import http.client
import io
import json
import threading
import unittest
from unittest.mock import patch

from luxembourg_mcp.providers import LuxembourgData
from luxembourg_mcp.server import McpServer

MODERN = "2026-07-28"


class ParkingHttp:
    def get_json_value(self, url, **kwargs):
        return [{"id": "p1", "name": "Mersch"}]


def meta(version=MODERN, capabilities=True):
    value = {"io.modelcontextprotocol/protocolVersion": version, "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"}}
    if capabilities:
        value["io.modelcontextprotocol/clientCapabilities"] = {}
    return value


def modern(method, request_id=1, version=MODERN, **params):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": {**params, "_meta": meta(version)}}


class ModernCoreTests(unittest.TestCase):
    def setUp(self):
        self.server = McpServer(LuxembourgData(ParkingHttp()))

    def test_discover_advertises_versions_capabilities_identity_and_cache_hints(self):
        result = self.server.handle(modern("server/discover"))["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertIn(MODERN, result["supportedVersions"])
        self.assertIn("2025-11-25", result["supportedVersions"])
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"], "luxembourg-mcp")
        self.assertIsInstance(result["ttlMs"], int)
        self.assertGreaterEqual(result["ttlMs"], 0)
        self.assertEqual(result["cacheScope"], "public")
        self.assertIn("38 tools", result["instructions"])

    def test_tools_list_is_cacheable_and_identifies_server(self):
        result = self.server.handle(modern("tools/list"))["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(len(result["tools"]), 38)
        self.assertEqual([tool["name"] for tool in result["tools"]], [tool["name"] for tool in self.server.handle(modern("tools/list"))["result"]["tools"]])
        self.assertGreater(result["ttlMs"], 0)
        self.assertEqual(result["cacheScope"], "public")
        self.assertIn("io.modelcontextprotocol/serverInfo", result["_meta"])

    def test_tools_call_success_and_tool_error_carry_result_type(self):
        ok = self.server.handle(modern("tools/call", name="list_cfl_parking", arguments={}))["result"]
        self.assertEqual(ok["resultType"], "complete")
        self.assertFalse(ok["isError"])
        self.assertEqual(ok["structuredContent"]["count"], 1)
        self.assertIn("io.modelcontextprotocol/serverInfo", ok["_meta"])
        bad = self.server.handle(modern("tools/call", name="get_traffic", arguments={"road": "zz"}))["result"]
        self.assertEqual(bad["resultType"], "complete")
        self.assertTrue(bad["isError"])

    def test_unknown_tool_is_invalid_params(self):
        response = self.server.handle(modern("tools/call", name="nope", arguments={}))
        self.assertEqual(response["error"]["code"], -32602)

    def test_unsupported_modern_version_lists_supported_versions(self):
        for version in ("2027-01-01", "2025-11-25"):
            with self.subTest(version=version):
                error = self.server.handle(modern("tools/list", version=version))["error"]
                self.assertEqual(error["code"], -32022)
                self.assertEqual(error["data"]["requested"], version)
                self.assertIn(MODERN, error["data"]["supported"])

    def test_missing_required_meta_fields_are_invalid_params(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": meta(capabilities=False)}}
        self.assertEqual(self.server.handle(request)["error"]["code"], -32602)
        request = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": 20260728, "io.modelcontextprotocol/clientCapabilities": {}}}}
        self.assertEqual(self.server.handle(request)["error"]["code"], -32602)

    def test_methods_removed_in_modern_era_are_not_found(self):
        for method in ("initialize", "ping", "resources/list", "subscriptions/listen"):
            with self.subTest(method=method):
                self.assertEqual(self.server.handle(modern(method))["error"]["code"], -32601)

    def test_legacy_initialize_never_negotiates_a_modern_version(self):
        response = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": MODERN}})
        self.assertEqual(response["result"]["protocolVersion"], "2025-11-25")
        self.assertNotIn("resultType", response["result"])

    def test_legacy_tools_list_is_unchanged(self):
        result = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]
        self.assertEqual(set(result), {"tools"})

    def test_stdio_serves_discover_probe_then_modern_requests(self):
        lines = [json.dumps(modern("server/discover", "probe")), json.dumps(modern("tools/list", 2))]
        stdout = io.StringIO()
        with patch("sys.stdin", io.StringIO("\n".join(lines) + "\n")), patch("sys.stdout", stdout):
            self.server.run_stdio()
        responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(responses[0]["id"], "probe")
        self.assertIn(MODERN, responses[0]["result"]["supportedVersions"])
        self.assertEqual(len(responses[1]["result"]["tools"]), 38)


class ModernHttpTests(unittest.TestCase):
    def setUp(self):
        self.httpd = McpServer(LuxembourgData(ParkingHttp())).create_http_server("127.0.0.1", 0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def send(self, body, headers=None, method="POST"):
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        try:
            payload = json.dumps(body).encode() if body is not None else None
            connection.request(method, "/mcp", body=payload, headers={"Content-Type": "application/json", **(headers or {})})
            response = connection.getresponse()
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None), response
        finally:
            connection.close()

    @staticmethod
    def headers(method, name=None, version=MODERN):
        value = {"MCP-Protocol-Version": version, "Mcp-Method": method}
        if name is not None:
            value["Mcp-Name"] = name
        return value

    def test_discover_with_matching_headers(self):
        status, body, _ = self.send(modern("server/discover"), self.headers("server/discover"))
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["resultType"], "complete")

    def test_tools_call_with_plain_and_base64_mcp_name(self):
        request = modern("tools/call", name="list_cfl_parking", arguments={})
        status, body, _ = self.send(request, self.headers("tools/call", "list_cfl_parking"))
        self.assertEqual((status, body["result"]["isError"]), (200, False))
        encoded = "=?base64?" + base64.b64encode(b"list_cfl_parking").decode() + "?="
        status, body, _ = self.send(request, self.headers("tools/call", encoded))
        self.assertEqual((status, body["result"]["isError"]), (200, False))

    def test_header_body_mismatches_are_rejected(self):
        request = modern("tools/call", name="list_cfl_parking", arguments={})
        cases = {
            "missing version header": {"Mcp-Method": "tools/call", "Mcp-Name": "list_cfl_parking"},
            "version mismatch": self.headers("tools/call", "list_cfl_parking", version="2025-11-25"),
            "missing method header": {"MCP-Protocol-Version": MODERN, "Mcp-Name": "list_cfl_parking"},
            "method mismatch": self.headers("tools/list", "list_cfl_parking"),
            "missing name header": self.headers("tools/call"),
            "name mismatch": self.headers("tools/call", "get_traffic"),
            "bad base64 name": self.headers("tools/call", "=?base64?%%%?="),
        }
        for label, headers in cases.items():
            with self.subTest(case=label):
                status, body, _ = self.send(request, headers)
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], -32020)
                self.assertEqual(body["id"], 1)

    def test_unsupported_modern_version_is_400_with_supported_list(self):
        status, body, _ = self.send(modern("server/discover", version="2027-01-01"), self.headers("server/discover", version="2027-01-01"))
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], -32022)
        self.assertIn(MODERN, body["error"]["data"]["supported"])

    def test_unknown_modern_method_is_404(self):
        status, body, _ = self.send(modern("prompts/list"), self.headers("prompts/list"))
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], -32601)

    def test_missing_meta_fields_are_400(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": meta(capabilities=False)}}
        status, body, _ = self.send(request, self.headers("tools/list"))
        self.assertEqual((status, body["error"]["code"]), (400, -32602))

    def test_modern_header_on_legacy_body_is_400(self):
        status, body, _ = self.send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, self.headers("tools/list"))
        self.assertEqual((status, body["error"]["code"]), (400, -32602))

    def test_unknown_header_version_on_legacy_body_is_unsupported_version(self):
        status, body, _ = self.send({"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"MCP-Protocol-Version": "1999-01-01"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], -32022)
        self.assertEqual(body["error"]["data"]["requested"], "1999-01-01")

    def test_legacy_handshake_flow_still_works(self):
        status, body, _ = self.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}})
        self.assertEqual((status, body["result"]["protocolVersion"]), (200, "2025-11-25"))
        status, body, _ = self.send({"jsonrpc": "2.0", "method": "notifications/initialized"}, {"MCP-Protocol-Version": "2025-11-25"})
        self.assertEqual((status, body), (202, None))
        status, body, _ = self.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, {"MCP-Protocol-Version": "2025-11-25"})
        self.assertEqual((status, len(body["result"]["tools"])), (200, 38))

    def test_delete_and_head_on_mcp_endpoint_are_method_not_allowed(self):
        for method in ("DELETE", "HEAD"):
            with self.subTest(method=method):
                status, _, response = self.send(None, method=method)
                self.assertEqual(status, 405)
                self.assertIn("POST", response.getheader("Allow"))

    def test_cors_preflight_allows_modern_request_headers(self):
        with patch.dict("os.environ", {"LUXEMBOURG_MCP_ALLOWED_ORIGINS": "*"}):
            httpd = McpServer(LuxembourgData(ParkingHttp())).create_http_server("127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
            connection.request("OPTIONS", "/mcp", headers={"Origin": "https://claude.ai"})
            allowed = connection.getresponse().getheader("Access-Control-Allow-Headers").lower()
            connection.close()
            for header in ("mcp-protocol-version", "mcp-method", "mcp-name"):
                self.assertIn(header, allowed)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
