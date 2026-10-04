"""Status lights: probe coverage, the degraded/down state machine, /status over HTTP, and HttpClient.probe."""

import http.client
import json
import re
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request

from luxembourg_mcp import status
from luxembourg_mcp.http import HttpClient, UpstreamError
from luxembourg_mcp.providers import LuxembourgData
from luxembourg_mcp.server import McpServer
from luxembourg_mcp.status import TOOL_UPSTREAMS, UPSTREAMS, StatusMonitor


class ProbeHttp:
    """Answers probes from a {upstream key: error message or None} map; records every probed URL."""

    def __init__(self, failing=None):
        self.failing = dict(failing or {})
        self.urls = []
        self._lock = threading.Lock()
        self._by_url = {url: key for key, (_, url, _) in UPSTREAMS.items()}

    def probe(self, url, headers=None, *, timeout=10, read_bytes=4096):
        with self._lock:
            self.urls.append(url)
        error = self.failing.get(self._by_url[url])
        if isinstance(error, Exception):
            raise error
        if error:
            raise UpstreamError(error)
        return 200


class FakeClock:
    def __init__(self, now=1_790_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class CoverageTests(unittest.TestCase):
    def test_every_registered_tool_has_upstreams(self):
        self.assertEqual(set(TOOL_UPSTREAMS), set(McpServer().tools))

    def test_every_upstream_is_defined_and_used(self):
        used = {key for keys in TOOL_UPSTREAMS.values() for key in keys}
        self.assertEqual(used, set(UPSTREAMS))

    def test_probe_urls_are_https_and_never_hit_the_download_host_for_files(self):
        for key, (name, url, headers) in UPSTREAMS.items():
            with self.subTest(upstream=key):
                parsed = urlsplit(url)
                self.assertEqual(parsed.scheme, "https")
                self.assertTrue(name)
                if parsed.hostname == "download.data.public.lu":
                    self.assertEqual(parsed.path, "/")

    def test_dataset_slugs_the_providers_fetch_are_all_probed(self):
        source = (Path(__file__).parents[1] / "src" / "luxembourg_mcp" / "providers.py").read_text(encoding="utf-8")
        constants = set(re.findall(r"^([A-Z_]+(?:_SLUG|_DATASET)) = ", source, re.M))
        from luxembourg_mcp import providers
        probed = {key.split(":", 1)[1] for key in UPSTREAMS if key.startswith("dataset:")}
        for constant in constants:
            with self.subTest(constant=constant):
                self.assertIn(getattr(providers, constant), probed)


class MonitorTests(unittest.TestCase):
    def test_first_snapshot_probes_every_upstream_once(self):
        http = ProbeHttp()
        snapshot = StatusMonitor(http).snapshot()
        self.assertEqual(sorted(http.urls), sorted(url for _, url, _ in UPSTREAMS.values()))
        self.assertEqual(snapshot["summary"], {"ok": len(TOOL_UPSTREAMS), "unknown": 0, "degraded": 0, "down": 0})
        self.assertEqual(snapshot["tools"]["get_traffic_events"], {"status": "ok", "upstreams": ["cita_events"]})
        self.assertTrue(snapshot["checked_at"].endswith("Z"))

    def test_one_failure_is_degraded_two_in_a_row_is_down_and_recovery_clears_it(self):
        http = ProbeHttp({"lod": "HTTP 500"})
        monitor = StatusMonitor(http, clock=FakeClock())
        monitor.refresh()
        first = monitor.snapshot()
        self.assertEqual(first["upstreams"]["lod"]["status"], "degraded")
        self.assertEqual(first["upstreams"]["lod"]["error"], "HTTP 500")
        self.assertIsNone(first["upstreams"]["lod"]["last_ok_at"])
        self.assertEqual(first["tools"]["lookup_luxembourgish"]["status"], "degraded")
        monitor.refresh()
        self.assertEqual(monitor.snapshot()["upstreams"]["lod"]["status"], "down")
        http.failing.clear()
        monitor.refresh()
        recovered = monitor.snapshot()["upstreams"]["lod"]
        self.assertEqual((recovered["status"], recovered["error"]), ("ok", None))
        self.assertIsNotNone(recovered["last_ok_at"])

    def test_last_ok_survives_later_failures(self):
        http = ProbeHttp()
        clock = FakeClock()
        monitor = StatusMonitor(http, clock=clock)
        monitor.refresh()
        first_ok = monitor.snapshot()["upstreams"]["velok"]["last_ok_at"]
        clock.now += 600
        http.failing["velok"] = "unreachable: timed out"
        monitor.refresh()
        state = monitor.snapshot()["upstreams"]["velok"]
        self.assertEqual(state["last_ok_at"], first_ok)
        self.assertNotEqual(state["checked_at"], first_ok)

    def test_tool_takes_the_worst_state_of_its_upstreams(self):
        http = ProbeHttp({"dataset:electricity-in-luxembourg-cross-border-physical-flows": "HTTP 404"})
        monitor = StatusMonitor(http)
        monitor.refresh()
        monitor.refresh()
        snapshot = monitor.snapshot()
        self.assertEqual(snapshot["tools"]["get_electricity_grid"]["status"], "down")
        self.assertEqual(snapshot["tools"]["get_electricity_prices"]["status"], "ok")
        self.assertEqual(snapshot["summary"]["down"], 1)

    def test_shared_upstream_outage_reaches_every_dependent_tool(self):
        monitor = StatusMonitor(ProbeHttp({"catalog": "HTTP 503"}))
        snapshot = monitor.snapshot()
        dependent = {name for name, keys in TOOL_UPSTREAMS.items() if "catalog" in keys}
        self.assertEqual({name for name, tool in snapshot["tools"].items() if tool["status"] == "degraded"}, dependent)

    def test_slow_answers_are_degraded(self):
        with patch.object(status, "SLOW_SECONDS", -1):
            snapshot = StatusMonitor(ProbeHttp()).snapshot()
        self.assertEqual(snapshot["summary"]["degraded"], len(TOOL_UPSTREAMS))

    def test_unexpected_probe_exceptions_are_contained(self):
        snapshot = StatusMonitor(ProbeHttp({"statec": ValueError("boom")})).snapshot()
        self.assertEqual(snapshot["upstreams"]["statec"]["error"], "probe failed: ValueError")
        self.assertEqual(snapshot["tools"]["get_statistics"]["status"], "degraded")

    def test_fresh_snapshots_do_not_probe_again(self):
        http = ProbeHttp()
        clock = FakeClock()
        monitor = StatusMonitor(http, clock=clock)
        monitor.snapshot()
        clock.now += status.REFRESH_SECONDS - 1
        monitor.snapshot()
        self.assertEqual(len(http.urls), len(UPSTREAMS))

    def test_stale_snapshot_answers_immediately_and_refreshes_in_background(self):
        release = threading.Event()

        class SlowSecondRound(ProbeHttp):
            def probe(self, url, headers=None, **kwargs):
                if len(self.urls) >= len(UPSTREAMS):
                    release.wait(5)
                return super().probe(url, headers, **kwargs)

        http = SlowSecondRound()
        clock = FakeClock()
        monitor = StatusMonitor(http, clock=clock)
        first = monitor.snapshot()
        clock.now += status.REFRESH_SECONDS
        stale = monitor.snapshot()  # must not block on the held-up second round
        self.assertEqual(stale["checked_at"], first["checked_at"])
        again = monitor.snapshot()  # a second stale read must not start another round
        self.assertEqual(again["checked_at"], first["checked_at"])
        release.set()
        for _ in range(100):
            if monitor.snapshot()["checked_at"] != first["checked_at"]:
                break
            threading.Event().wait(0.05)
        self.assertNotEqual(monitor.snapshot()["checked_at"], first["checked_at"])
        self.assertEqual(len(http.urls), 2 * len(UPSTREAMS))


class HangingHttp(ProbeHttp):
    """ProbeHttp whose listed upstreams block until released, like a server that accepts and never answers."""

    def __init__(self, hanging):
        super().__init__()
        self.hanging = set(hanging)
        self.release = threading.Event()

    def probe(self, url, headers=None, **kwargs):
        if self._by_url[url] in self.hanging:
            self.release.wait(10)
        return super().probe(url, headers, **kwargs)


class SlowUpstreamTests(unittest.TestCase):
    def test_first_visit_answers_unknown_instead_of_queueing_behind_slow_upstreams(self):
        http = HangingHttp(UPSTREAMS)
        self.addCleanup(http.release.set)
        with patch.object(status, "FIRST_WAIT_SECONDS", 0.2):
            started = time.monotonic()
            snapshot = StatusMonitor(http).snapshot()
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2)
        self.assertIsNone(snapshot["checked_at"])
        self.assertEqual(snapshot["summary"]["unknown"], len(TOOL_UPSTREAMS))

    def test_concurrent_first_visits_start_a_single_round(self):
        http = HangingHttp({"lod"})
        self.addCleanup(http.release.set)
        monitor = StatusMonitor(http)
        with patch.object(status, "FIRST_WAIT_SECONDS", 0.2):
            threads = [threading.Thread(target=monitor.snapshot) for _ in range(5)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
        http.release.set()
        for _ in range(100):
            if monitor.snapshot()["checked_at"]:
                break
            time.sleep(0.05)
        self.assertEqual(len(http.urls), len(UPSTREAMS))

    def test_round_deadline_marks_hung_upstreams_and_keeps_the_rest(self):
        http = HangingHttp({"lod"})
        self.addCleanup(http.release.set)
        with patch.object(status, "ROUND_DEADLINE_SECONDS", 0.3):
            monitor = StatusMonitor(http)
            started = time.monotonic()
            monitor.refresh()
            self.assertLess(time.monotonic() - started, 3)
        snapshot = monitor.snapshot()
        self.assertEqual((snapshot["upstreams"]["lod"]["status"], snapshot["upstreams"]["lod"]["error"]),
                         ("degraded", "check timed out"))
        self.assertEqual(snapshot["upstreams"]["velok"]["status"], "ok")
        self.assertEqual(snapshot["summary"]["degraded"], 1)


class StatusEndpointTests(unittest.TestCase):
    def serve(self, server):
        httpd = server.create_http_server("127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close(), thread.join(timeout=5)))
        return httpd.server_address[1]

    def request(self, port, method, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response, body

    def server_with(self, probe_http):
        return McpServer(LuxembourgData(probe_http))

    def test_get_status_is_public_json(self):
        port = self.serve(self.server_with(ProbeHttp({"lod": "HTTP 500"})))
        response, body = self.request(port, "GET", "/status", {"Origin": "https://luxembourg-mcp.com"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), "*")
        self.assertEqual(response.getheader("Cache-Control"), "public, max-age=60")
        data = json.loads(body)
        self.assertEqual(set(data["tools"]), set(TOOL_UPSTREAMS))
        self.assertEqual(data["tools"]["lookup_luxembourgish"]["status"], "degraded")

    def test_head_status_is_allowed(self):
        port = self.serve(self.server_with(ProbeHttp()))
        response, _ = self.request(port, "HEAD", "/status")
        self.assertEqual(response.status, 200)

    def test_status_failure_is_a_503_not_a_crash(self):
        server = self.server_with(ProbeHttp())
        with patch.object(McpServer, "status", side_effect=RuntimeError("boom")):
            port = self.serve(server)
            response, body = self.request(port, "GET", "/status")
        self.assertEqual(response.status, 503)
        self.assertIn("unavailable", json.loads(body)["error"])

    def test_monitor_is_created_once_and_reuses_the_provider_client(self):
        probe_http = ProbeHttp()
        server = self.server_with(probe_http)
        server.status()
        server.status()
        self.assertEqual(len(probe_http.urls), len(UPSTREAMS))


class FakeResponse:
    status = 200

    def __init__(self):
        self.requested = []

    def read(self, size=-1):
        self.requested.append(size)
        return b"x" * max(size, 0)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class HttpProbeTests(unittest.TestCase):
    def test_reads_only_the_start_of_the_body(self):
        response = FakeResponse()

        class Opener:
            def open(self, request, timeout=None):
                self.timeout = timeout
                return response

        opener = Opener()
        with patch("luxembourg_mcp.http.build_opener", return_value=opener):
            self.assertEqual(HttpClient().probe("https://api.example.test/x", timeout=3), 200)
        self.assertEqual(response.requested, [4096])
        self.assertEqual(opener.timeout, 3)

    def test_http_errors_and_network_failures_become_upstream_errors(self):
        failures = [
            HTTPError("https://api.example.test/x", 503, "Unavailable", {}, None),
            TimeoutError("timed out"),
            ConnectionResetError("reset"),
            http.client.IncompleteRead(b""),
        ]
        for failure in failures:
            class Opener:
                def open(self, request, timeout=None, failure=failure):
                    raise failure

            with self.subTest(failure=type(failure).__name__), \
                    patch("luxembourg_mcp.http.build_opener", return_value=Opener()), \
                    self.assertRaises(UpstreamError):
                HttpClient().probe("https://api.example.test/x")

    def test_probe_only_follows_same_host_https_redirects(self):
        handlers = []

        def fake_build_opener(handler):
            handlers.append(handler)

            class Opener:
                def open(self, request, timeout=None):
                    return FakeResponse()
            return Opener()

        with patch("luxembourg_mcp.http.build_opener", side_effect=fake_build_opener):
            HttpClient().probe("https://api.example.test/x")
        with self.assertRaises(UpstreamError):
            handlers[0].redirect_request(Request("https://api.example.test/x"), None, 302, "Found", {}, "https://evil.example/x")

    def test_rejects_plain_http(self):
        with self.assertRaises(UpstreamError):
            HttpClient().probe("http://api.example.test/x")


if __name__ == "__main__":
    unittest.main()
