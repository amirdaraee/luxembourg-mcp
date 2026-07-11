import unittest

from luxembourg_mcp.server import McpServer


TOOL_CASES = {
    "search_datasets": {"query": "weather", "page_size": 2},
    "get_dataset": {"dataset_id_or_slug": "niveau-deau"},
    "geocode_address": {"query": "54 Avenue Gaston Diderich, Luxembourg"},
    "reverse_geocode": {"latitude": 49.61055, "longitude": 6.11249},
    "list_geo_collections": {"query": "water", "limit": 2},
    "get_geo_features": {"collection_id": "655", "limit": 1},
    "get_weather_alerts": {"language": "en"},
    "search_legislation": {"query": "pension", "limit": 2},
    "search_statistics": {"query": "population", "limit": 2},
    "get_statistics": {"dataflow_id": "DF_D7100", "last_n_observations": 1, "max_rows": 5},
    "get_city_parking": {"available_only": True},
    "list_cfl_parking": {},
    "get_cfl_parking": {"parking_id": "RDWRW"},
    "get_traffic": {"road": "a6"},
    "get_water_levels": {"station": "Mersch"},
    "get_air_quality": {"city": "Luxembourg"},
    "search_chamber_bodies": {"query": "Pétitions", "limit": 2},
    "get_accessibility_figures": {},
    "get_accessibility_audits": {"limit": 2},
    "search_transit_stops": {"query": "Hamilius", "limit": 3},
    "get_city_mobility": {"category": "bike_rentals"},
    "get_weather_observations": {},
    "get_public_holidays": {"year": 2026},
    "search_parliamentary_questions": {"query": "logement", "limit": 2},
    "get_housing_prices": {"property_type": "apartment", "commune": "Bertrange"},
    "get_election_results": {},
    "get_ev_charging": {"query": "Esch", "available_only": True},
}


class RecordingData:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(**arguments):
            self.calls.append((name, arguments))
            return {"tool": name, "arguments": arguments, "source": f"https://example.test/{name}"}

        return call


class EveryToolContractTests(unittest.TestCase):
    def setUp(self):
        self.data = RecordingData()
        self.server = McpServer(self.data)

    def test_every_registered_tool_has_a_contract_case(self):
        self.assertEqual(set(self.server.tools), set(TOOL_CASES))
        self.assertEqual(len(TOOL_CASES), 27)

    def test_every_tool_routes_arguments_and_returns_structured_content(self):
        for request_id, (name, arguments) in enumerate(TOOL_CASES.items(), start=1):
            with self.subTest(tool=name):
                response = self.server.handle({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                })
                self.assertNotIn("error", response)
                result = response["result"]
                self.assertFalse(result["isError"])
                self.assertEqual(result["structuredContent"]["tool"], name)
                self.assertEqual(result["structuredContent"]["arguments"], arguments)
                self.assertEqual(self.data.calls[-1], (name, arguments))
                self.assertEqual(result["content"][0]["type"], "text")


if __name__ == "__main__":
    unittest.main()

