from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from src.profiling import ProfilingConfig, run_profiling


class ProfilingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.raw = self.root / "raw"
        self.results = self.root / "results" / "profiling"
        self.manifest = self.root / "dataset_manifest.yaml"
        self.raw.mkdir()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_manifest(self, file_names: list[str]) -> None:
        manifest = {
            "dataset": {
                "physical_format": {
                    "delimiter": ";",
                    "header": True,
                    "text_qualifier": '"',
                },
                "files": [
                    {
                        "name": file_name,
                        "expected_record_count": 0,
                        "sha256": "0" * 64,
                    }
                    for file_name in file_names
                ],
            }
        }
        self.manifest.write_text(
            yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
        )

    def run_test_profiling(self) -> dict[str, object]:
        return run_profiling(
            ProfilingConfig(
                raw_path=self.raw,
                manifest_path=self.manifest,
                results_path=self.results,
            )
        )

    def get_column(
        self, execution: dict[str, object], column_name: str
    ) -> dict[str, object]:
        columns = execution["tables"][0]["columns"]
        return next(column for column in columns if column["column_name"] == column_name)

    def assert_execution_record(self, execution: dict[str, object]) -> None:
        records = list(self.results.glob("*.json"))
        self.assertEqual(len(records), 1)
        stored = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertEqual(stored["run_id"], execution["run_id"])
        self.assertEqual(stored["status"], execution["status"])

    def test_profiles_rows_columns_distinct_values_and_ratio(self) -> None:
        content = (
            b'id;value;all_empty\n'
            b'1;alpha;\n'
            b'2;beta;""\n'
            b'2;alpha;\n'
        )
        raw_file = self.raw / "fixture.asc"
        raw_file.write_bytes(content)
        self.write_manifest(["fixture.asc"])

        execution = self.run_test_profiling()

        self.assertEqual(execution["status"], "SUCCESS")
        self.assertEqual(execution["files_profiled"], 1)
        self.assertEqual(execution["tables"][0]["row_count"], 3)
        self.assertEqual(execution["tables"][0]["column_count"], 3)
        identifier = self.get_column(execution, "id")
        self.assertEqual(identifier["distinct_count"], 2)
        self.assertAlmostEqual(identifier["uniqueness_ratio"], 2 / 3)
        self.assertEqual(identifier["min_observed"], "1")
        self.assertEqual(identifier["max_observed"], "2")
        self.assertEqual(identifier["sample_values"], ["1", "2"])
        self.assert_execution_record(execution)

    def test_quoted_and_unquoted_empty_fields_share_physical_empty_bucket(self) -> None:
        content = b'id;value\n1;\n2;""\n3;text\n'
        (self.raw / "fixture.asc").write_bytes(content)
        self.write_manifest(["fixture.asc"])

        execution = self.run_test_profiling()
        value = self.get_column(execution, "value")

        self.assertEqual(value["total_count"], 3)
        self.assertEqual(value["empty_count"], 2)
        self.assertEqual(value["non_empty_count"], 1)
        self.assertEqual(value["parser_null_count"], 0)
        self.assertEqual(value["distinct_count"], 1)

    def test_zero_non_empty_values_has_null_uniqueness_ratio(self) -> None:
        (self.raw / "fixture.asc").write_bytes(b'id;empty\n1;\n2;""\n')
        self.write_manifest(["fixture.asc"])

        execution = self.run_test_profiling()
        empty = self.get_column(execution, "empty")

        self.assertEqual(execution["status"], "SUCCESS")
        self.assertEqual(empty["non_empty_count"], 0)
        self.assertEqual(empty["distinct_count"], 0)
        self.assertIsNone(empty["uniqueness_ratio"])
        self.assertIsNone(empty["min_observed"])
        self.assertIsNone(empty["max_observed"])
        self.assertEqual(empty["sample_values"], [])

    def test_missing_manifest_file_fails_clearly(self) -> None:
        self.write_manifest(["missing.asc"])

        execution = self.run_test_profiling()

        self.assertEqual(execution["status"], "FAILED")
        self.assertEqual(execution["files_expected"], 1)
        self.assertEqual(execution["files_profiled"], 0)
        self.assertIn("missing.asc", execution["error"])
        self.assert_execution_record(execution)

    def test_profiling_does_not_modify_raw(self) -> None:
        content = b'id;value\n1;alpha\n2;beta\n'
        raw_file = self.raw / "fixture.asc"
        raw_file.write_bytes(content)
        self.write_manifest(["fixture.asc"])
        before_bytes = raw_file.read_bytes()
        before_timestamp = raw_file.stat().st_mtime_ns
        before_names = sorted(path.name for path in self.raw.iterdir())

        execution = self.run_test_profiling()

        self.assertEqual(execution["status"], "SUCCESS")
        self.assertEqual(raw_file.read_bytes(), before_bytes)
        self.assertEqual(raw_file.stat().st_mtime_ns, before_timestamp)
        self.assertEqual(
            sorted(path.name for path in self.raw.iterdir()), before_names
        )


if __name__ == "__main__":
    unittest.main()

