from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from src.catalog import CatalogConfig, run_catalog_validation


class CatalogValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.manifest = self.root / "dataset_manifest.yaml"
        self.catalog = self.root / "catalog"
        self.relationships = self.root / "relationships.yaml"
        self.raw = self.root / "raw"
        self.results = self.root / "results" / "catalog"
        self.catalog.mkdir()
        self.raw.mkdir()
        self.write_manifest(["alpha.asc"])
        (self.raw / "alpha.asc").write_text("id;value\n1;one\n", encoding="utf-8")
        self.write_relationships([])

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def evidence() -> dict[str, str]:
        return {
            "type": "source_documentation",
            "document": "fixture-source-document.pdf",
        }

    def write_manifest(self, file_names: list[str]) -> None:
        document = {
            "dataset": {
                "physical_format": {
                    "delimiter": ";",
                    "header": True,
                    "text_qualifier": '"',
                },
                "files": [
                    {
                        "name": file_name,
                        "expected_record_count": 1,
                        "sha256": "0" * 64,
                    }
                    for file_name in file_names
                ],
            }
        }
        self.manifest.write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )

    def write_asset(
        self,
        name: str = "alpha",
        source_file: str = "alpha.asc",
        columns: list[str] | None = None,
        catalog_file: str = "alpha.yaml",
    ) -> None:
        if columns is None:
            columns = ["id", "value"]
        document = {
            "asset": {
                "name": name,
                "source_file": source_file,
                "description": "Technical fixture asset.",
                "evidence": self.evidence(),
            },
            "columns": [
                {
                    "name": column,
                    "description": "Technical fixture column.",
                    "evidence": self.evidence(),
                }
                for column in columns
            ],
        }
        (self.catalog / catalog_file).write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )

    def write_relationships(self, relationships: list[dict[str, object]]) -> None:
        self.relationships.write_text(
            yaml.safe_dump({"relationships": relationships}, sort_keys=False),
            encoding="utf-8",
        )

    def relationship(
        self, from_asset: str, from_column: str, to_asset: str, to_column: str
    ) -> dict[str, object]:
        return {
            "name": "fixture_relationship",
            "from": {"asset": from_asset, "column": from_column},
            "to": {"asset": to_asset, "column": to_column},
            "evidence": self.evidence(),
        }

    def run_validation(self) -> dict[str, object]:
        return run_catalog_validation(
            CatalogConfig(
                manifest_path=self.manifest,
                catalog_path=self.catalog,
                relationships_path=self.relationships,
                raw_path=self.raw,
                results_path=self.results,
            )
        )

    def messages(self, execution: dict[str, object]) -> str:
        return "\n".join(error["message"] for error in execution["errors"])

    def assert_execution_record(self, execution: dict[str, object]) -> None:
        records = list(self.results.glob("*.json"))
        self.assertEqual(len(records), 1)
        stored = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertEqual(stored["run_id"], execution["run_id"])
        self.assertEqual(stored["status"], execution["status"])

    def test_valid_catalog_succeeds(self) -> None:
        self.write_asset()
        self.write_relationships(
            [self.relationship("alpha", "id", "alpha", "value")]
        )

        execution = self.run_validation()

        self.assertEqual(execution["status"], "SUCCESS")
        self.assertEqual(execution["assets_expected"], 1)
        self.assertEqual(execution["assets_validated"], 1)
        self.assertEqual(execution["relationships_validated"], 1)
        self.assertEqual(execution["errors"], [])
        self.assert_execution_record(execution)

    def test_missing_catalog_asset_fails(self) -> None:
        execution = self.run_validation()

        self.assertEqual(execution["status"], "FAILED")
        self.assertIn("Expected exactly one catalog asset for alpha.asc", self.messages(execution))

    def test_undeclared_source_file_fails(self) -> None:
        self.write_asset(name="ghost", source_file="ghost.asc")

        execution = self.run_validation()

        self.assertEqual(execution["status"], "FAILED")
        self.assertIn("references undeclared source_file ghost.asc", self.messages(execution))

    def test_duplicate_catalog_column_fails(self) -> None:
        self.write_asset(columns=["id", "value", "value"])

        execution = self.run_validation()

        self.assertEqual(execution["status"], "FAILED")
        self.assertIn("duplicate column value", self.messages(execution))

    def test_catalog_column_absent_from_raw_fails(self) -> None:
        self.write_asset(columns=["id", "value", "extra"])

        execution = self.run_validation()

        self.assertEqual(execution["status"], "FAILED")
        self.assertIn("Catalog column alpha.extra is absent from RAW header", self.messages(execution))

    def test_raw_column_absent_from_catalog_fails(self) -> None:
        self.write_asset(columns=["id"])

        execution = self.run_validation()

        self.assertEqual(execution["status"], "FAILED")
        self.assertIn("RAW column alpha.asc.value is absent from catalog", self.messages(execution))

    def test_relationship_with_unknown_asset_fails(self) -> None:
        self.write_asset()
        self.write_relationships(
            [self.relationship("alpha", "id", "missing", "id")]
        )

        execution = self.run_validation()

        self.assertEqual(execution["status"], "FAILED")
        self.assertIn("references unknown asset missing", self.messages(execution))

    def test_relationship_with_unknown_column_fails(self) -> None:
        self.write_asset()
        self.write_relationships(
            [self.relationship("alpha", "id", "alpha", "missing")]
        )

        execution = self.run_validation()

        self.assertEqual(execution["status"], "FAILED")
        self.assertIn("references unknown column alpha.missing", self.messages(execution))


if __name__ == "__main__":
    unittest.main()

