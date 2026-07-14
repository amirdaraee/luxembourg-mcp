import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from luxembourg_mcp.cli import compact, main
from luxembourg_mcp.providers import LuxembourgData


class FakeHttp:
    def __init__(self, responses):
        self.responses = responses

    def get_json(self, url):
        return self.responses.pop(0)


def dataset_provider():
    return LuxembourgData(FakeHttp([{
        "total": 2,
        "data": [
            {"id": "1", "slug": "roads", "title": "Roads"},
            {"id": "2", "slug": "rails", "title": "Rails"},
        ],
    }]))  # type: ignore[arg-type]


class CliTests(unittest.TestCase):
    def test_calls_registered_tool_and_compacts_list_output(self):
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            exit_code = main(
                [
                    "search_datasets",
                    '{"query":"transport"}',
                    "--output-limit",
                    "1",
                ],
                data=dataset_provider(),
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

    def test_list_uses_common_output_controls(self):
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            exit_code = main(["list", "--output-limit", "1"])

        result = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["count"], 27)
        self.assertEqual(len(result["tools"]), 1)
        self.assertEqual(result["available_items"]["tools"], 27)

    def test_schema_validation_errors_return_nonzero(self):
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            exit_code = main(
                ["search_datasets", "{}"],
                data=dataset_provider(),
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("missing required field: query", stderr.getvalue())

    def test_save_is_atomic_and_output_modes_are_exclusive(self):
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "catalog.json"
            destination.write_text("old")
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main(["list", "--save", str(destination)])

            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(destination.read_text())["count"], 27)
            self.assertEqual([path.name for path in Path(directory).iterdir()], ["catalog.json"])
            self.assertEqual(json.loads(stdout.getvalue())["saved"], str(destination))

            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as error:
                main(["list", "--save", str(destination), "--summary-only"])
            self.assertEqual(error.exception.code, 2)

    def test_save_errors_return_nonzero_without_traceback(self):
        with TemporaryDirectory() as directory:
            parent_file = Path(directory) / "not-a-directory"
            parent_file.write_text("occupied")
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                exit_code = main(["list", "--save", str(parent_file / "result.json")])

        self.assertEqual(exit_code, 2)
        self.assertIn("Could not save result", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
