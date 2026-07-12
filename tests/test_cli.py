import io
import json
import unittest
from contextlib import redirect_stdout

from luxembourg_mcp.cli import compact, main
from luxembourg_mcp.providers import LuxembourgData


class FakeHttp:
    def __init__(self, responses):
        self.responses = responses

    def get_json(self, url):
        return self.responses.pop(0)


class CliTests(unittest.TestCase):
    def test_calls_registered_tool_and_compacts_list_output(self):
        provider = LuxembourgData(FakeHttp([{
            "total": 2,
            "data": [
                {"id": "1", "slug": "roads", "title": "Roads"},
                {"id": "2", "slug": "rails", "title": "Rails"},
            ],
        }]))
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            exit_code = main(
                ["search_datasets", '{"query":"transport"}', "--limit", "1"],
                data=provider,
            )

        result = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual([item["title"] for item in result["datasets"]], ["Roads"])
        self.assertEqual(result["returned_items"]["datasets"], 1)
        self.assertEqual(result["available_items"]["datasets"], 2)
        self.assertIn("data.public.lu", result["source"])

    def test_compaction_preserves_nested_structural_lists(self):
        result = compact({
            "features": [{
                "geometry": {
                    "type": "Point",
                    "coordinates": [6.13, 49.61],
                },
            }],
        }, limit=1)

        self.assertEqual(
            result["features"][0]["geometry"]["coordinates"],
            [6.13, 49.61],
        )


if __name__ == "__main__":
    unittest.main()
