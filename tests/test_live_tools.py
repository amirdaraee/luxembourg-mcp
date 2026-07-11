import os
import unittest

from luxembourg_mcp.server import McpServer


LIVE = os.environ.get("LUXEMBOURG_MCP_LIVE") == "1"


@unittest.skipUnless(LIVE, "set LUXEMBOURG_MCP_LIVE=1 to call official upstream services")
class EveryToolLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = McpServer()
        cls.request_id = 0

    def call(self, name, arguments=None):
        type(self).request_id += 1
        response = self.server.handle({
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })
        self.assertNotIn("error", response)
        result = response["result"]
        self.assertFalse(result["isError"], result["content"][0]["text"])
        data = result.get("structuredContent")
        self.assertIsInstance(data, dict)
        self.assertTrue(str(data.get("source", "")).startswith("https://"))
        return data

    def test_search_datasets(self):
        self.assertGreaterEqual(self.call("search_datasets", {"query": "weather", "page_size": 2})["total"], 1)

    def test_get_dataset(self):
        self.assertEqual(self.call("get_dataset", {"dataset_id_or_slug": "niveau-deau"})["slug"], "niveau-deau")

    def test_geocode_address(self):
        self.assertGreaterEqual(self.call("geocode_address", {"query": "54 Avenue Gaston Diderich, Luxembourg"})["count"], 1)

    def test_reverse_geocode(self):
        self.assertGreaterEqual(self.call("reverse_geocode", {"latitude": 49.61055, "longitude": 6.11249})["count"], 1)

    def test_list_geo_collections(self):
        self.assertGreaterEqual(self.call("list_geo_collections", {"query": "water", "limit": 2})["total"], 1)

    def test_get_geo_features(self):
        self.assertIn("features", self.call("get_geo_features", {"collection_id": "655", "limit": 1}))

    def test_get_weather_alerts(self):
        self.assertIn("alerts", self.call("get_weather_alerts", {"language": "en"}))

    def test_search_legislation(self):
        self.assertGreaterEqual(self.call("search_legislation", {"query": "pension", "limit": 2})["count"], 1)

    def test_search_statistics(self):
        self.assertGreaterEqual(self.call("search_statistics", {"query": "population", "limit": 2})["count"], 1)

    def test_get_statistics(self):
        self.assertGreaterEqual(self.call("get_statistics", {"dataflow_id": "DF_D7100", "last_n_observations": 1, "max_rows": 5})["count"], 1)

    def test_get_city_parking(self):
        self.assertIn("parking", self.call("get_city_parking", {"available_only": True}))

    def test_list_cfl_parking(self):
        self.assertGreaterEqual(self.call("list_cfl_parking")["count"], 1)

    def test_get_cfl_parking(self):
        self.assertEqual(self.call("get_cfl_parking", {"parking_id": "RDWRW"})["parking"]["id"], "RDWRW")

    def test_get_traffic(self):
        self.assertGreaterEqual(self.call("get_traffic", {"road": "a6"})["count"], 1)

    def test_get_water_levels(self):
        self.assertGreaterEqual(self.call("get_water_levels", {"station": "Mersch"})["count"], 1)

    def test_get_air_quality(self):
        self.assertIn("stations", self.call("get_air_quality", {"city": "Luxembourg"}))

    def test_search_chamber_bodies(self):
        self.assertGreaterEqual(self.call("search_chamber_bodies", {"query": "Pétitions", "limit": 2})["count"], 1)

    def test_get_accessibility_figures(self):
        self.assertIn("total_audits", self.call("get_accessibility_figures")["figures"])

    def test_get_accessibility_audits(self):
        self.assertIn("audits", self.call("get_accessibility_audits", {"limit": 2}))

    def test_search_transit_stops(self):
        self.assertGreaterEqual(self.call("search_transit_stops", {"query": "Hamilius", "limit": 3})["count"], 1)

    def test_get_city_mobility(self):
        self.assertGreaterEqual(self.call("get_city_mobility", {"category": "bike_rentals"})["count"], 1)


if __name__ == "__main__":
    unittest.main()
