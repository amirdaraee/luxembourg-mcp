import io
import json
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from luxembourg_mcp.providers import LuxembourgData
from luxembourg_mcp.server import McpServer, catalog_html


class FakeHttp:
    def __init__(self, responses):
        self.responses = responses
        self.urls = []

    def get_json(self, url):
        self.urls.append(url)
        return self.responses.pop(0)

    def get_json_value(self, url, **kwargs):
        self.urls.append(url)
        return self.responses.pop(0)

    def get_bytes(self, url, headers=None, **kwargs):
        self.urls.append(url)
        return self.responses.pop(0), "utf-8"


class ProviderTests(unittest.TestCase):
    def test_search_datasets_shapes_results(self):
        http = FakeHttp([{"total": 1, "data": [{"id": "abc", "slug": "roads", "title": "Roads", "organization": {"name": "CITA"}}]}])
        result = LuxembourgData(http).search_datasets("roads")
        self.assertEqual(result["datasets"][0]["organization"], "CITA")
        self.assertIn("q=roads", result["source"])

    def test_reverse_geocode_rejects_remote_coordinates(self):
        with self.assertRaises(ValueError):
            LuxembourgData(FakeHttp([])).reverse_geocode(48.85, 2.35)

    def test_geo_collection_filter(self):
        http = FakeHttp([{"collections": [{"id": "1", "title": "Cycle paths", "keywords": ["mobility", 2026]}, {"id": "2", "title": "Forests"}]}])
        result = LuxembourgData(http).list_geo_collections("cycle")
        self.assertEqual([item["id"] for item in result["collections"]], ["1"])

    def test_water_levels_station_filter_ignores_accents(self):
        payload = "Name;Ettelbrück / Alzette;Remich\nNumber;7;8\nUnit;cm;cm\n10.07.2026 20:00;68.4;349\n".encode()
        result = LuxembourgData(FakeHttp([payload])).get_water_levels("Ettelbruck")
        self.assertEqual([item["name"] for item in result["stations"]], ["Ettelbrück / Alzette"])

    def test_weather_alerts_skip_excel_preamble(self):
        dataset = {
            "id": "x", "slug": "meteolux", "title": "alerts",
            "resources": [{"id": "1", "title": "en-data-alerts.csv", "format": "csv",
                           "url": "https://download.data.public.lu/resources/en-data-alerts.csv"}],
        }
        csv_payload = (b"sep=;\ncreated;11-07-2026 13:29:53\n"
                       b"NORTH;SOUTH;LEGEND;DESCRIPTION\nfalse;true;Potential Risk;Heat warning\n")
        result = LuxembourgData(FakeHttp([dataset, csv_payload])).get_weather_alerts("en")
        self.assertEqual(result["created"], "11-07-2026 13:29:53")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["alerts"][0]["DESCRIPTION"], "Heat warning")

    def test_water_levels_returns_latest_filtered_station(self):
        payload = b"Unit;cm;cm\nName;Mersch;Remich\nNumber;7;8\n10.07.2026 20:00;68.4;349\n10.07.2026 19:45;67.9;348\n\n"
        result = LuxembourgData(FakeHttp([payload])).get_water_levels("Mersch")
        self.assertEqual(result["stations"], [{"name": "Mersch", "station_number": "7", "unit": "cm", "value": 68.4}])
        self.assertEqual(result["measured_at"], "10.07.2026 20:00")

    def test_traffic_parses_namespaced_datex(self):
        payload = b'''<d2LogicalModel xmlns="http://datex2.eu/schema/2/2_0"><siteMeasurements>
          <measurementSiteReference id="A6.TEST"/><measurementTimeDefault>2026-07-10T20:00:00+02:00</measurementTimeDefault>
          <roadNumber>A6</roadNumber><latitude>49.6</latitude><longitude>6.1</longitude>
          <speed>90.0</speed><percentage>12.5</percentage><vehicleFlowRate>375</vehicleFlowRate>
        </siteMeasurements></d2LogicalModel>'''
        result = LuxembourgData(FakeHttp([payload])).get_traffic("a6")
        self.assertEqual(result["stations"][0]["average_speed_kmh"], 90)
        self.assertEqual(result["stations"][0]["vehicle_flow_per_hour"], 375)

    def test_city_parking_filters_available_spaces(self):
        http = FakeHttp([{"last_build_date": "now", "parking": {"1": {"titre": "Open", "ouvert": True, "actuel": 5}, "2": {"titre": "Full", "ouvert": True, "actuel": 0}}}])
        result = LuxembourgData(http).get_city_parking(available_only=True)
        self.assertEqual([item["titre"] for item in result["parking"]], ["Open"])


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.server = McpServer(LuxembourgData(FakeHttp([])))

    def test_initialize(self):
        response = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}})
        self.assertEqual(response["result"]["protocolVersion"], "2025-11-25")

    def test_initialize_echoes_supported_older_version(self):
        response = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(response["result"]["protocolVersion"], "2025-06-18")

    def test_initialize_proposes_latest_for_unknown_version(self):
        response = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}})
        self.assertEqual(response["result"]["protocolVersion"], "2025-11-25")

    def test_lists_twenty_one_tools(self):
        response = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(len(response["result"]["tools"]), 21)

    def test_notifications_have_no_response(self):
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_non_object_json_is_invalid_request(self):
        for value in (123, [1, 2], "request", None):
            with self.subTest(value=value):
                response = self.server.handle(value)
                self.assertEqual(response["error"]["code"], -32600)

    def test_stdio_survives_non_object_json(self):
        stdin = io.StringIO('123\n{"jsonrpc":"2.0","id":2,"method":"ping"}\n')
        stdout = io.StringIO()
        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            self.server.run_stdio()
        responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(responses[0]["error"]["code"], -32600)
        self.assertEqual(responses[1]["result"], {})

    def test_schema_rejects_wrong_type_extra_field_enum_and_range(self):
        cases = [
            ("search_datasets", {"query": 123}),
            ("search_datasets", {"query": "water", "extra": True}),
            ("get_weather_alerts", {"language": "es"}),
            ("search_datasets", {"query": "water", "page_size": 51}),
        ]
        for index, (name, arguments) in enumerate(cases, start=10):
            with self.subTest(tool=name, arguments=arguments):
                response = self.server.handle({"jsonrpc": "2.0", "id": index, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
                self.assertTrue(response["result"]["isError"])
                self.assertTrue(response["result"]["content"][0]["text"].startswith("Invalid arguments:"))

    def test_http_rejects_scalar_json_and_unsupported_protocol(self):
        httpd = self.server.create_http_server("127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{httpd.server_address[1]}/mcp"
        try:
            scalar = Request(endpoint, data=b"123", headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(scalar, timeout=5) as response:
                body = json.load(response)
            self.assertEqual(body["error"]["code"], -32600)

            unsupported = Request(
                endpoint,
                data=b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
                headers={"Content-Type": "application/json", "MCP-Protocol-Version": "1999-01-01"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as caught:
                urlopen(unsupported, timeout=5)
            error = caught.exception
            try:
                self.assertEqual(error.code, 400)
                self.assertEqual(json.load(error)["error"]["message"], "Unsupported protocol version")
            finally:
                error.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_bad_tool_arguments_are_tool_errors(self):
        response = self.server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "reverse_geocode", "arguments": {"latitude": 0, "longitude": 0}}})
        self.assertTrue(response["result"]["isError"])

    def test_catalog_is_packaged(self):
        page = catalog_html().decode("utf-8")
        self.assertIn("Luxembourg MCP", page)
        self.assertIn("search_datasets", page)
        self.assertEqual(page.count('class="tool-card"'), 21)
        self.assertIn("<strong>15</strong> official systems", page)


if __name__ == "__main__":
    unittest.main()
