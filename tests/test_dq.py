from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from src.dq import DQConfig, run_dq


class DQTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.manifest = self.root / "dataset_manifest.yaml"
        self.catalog = self.root / "catalog"
        self.relationships = self.root / "relationships.yaml"
        self.rules = self.root / "rules"
        self.raw = self.root / "raw"
        self.results = self.root / "results" / "dq"
        self.catalog.mkdir()
        self.rules.mkdir()
        self.raw.mkdir()
        self.write_manifest()
        self.write_catalog()
        self.write_relationships()
        self.write_raw(b"id;value\n1;A\n2;B\n", b"id\nA\nB\n")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def source_evidence() -> dict[str, str]:
        return {"type": "source_documentation", "description": "Fixture expectation."}

    def write_manifest(self) -> None:
        document = {
            "dataset": {
                "physical_format": {
                    "delimiter": ";",
                    "header": True,
                    "text_qualifier": '"',
                },
                "files": [
                    {"name": name, "expected_record_count": 0, "sha256": "0" * 64}
                    for name in ("alpha.asc", "reference.asc")
                ],
            }
        }
        self.manifest.write_text(yaml.safe_dump(document), encoding="utf-8")

    def write_catalog(self) -> None:
        evidence = {
            "type": "source_documentation",
            "document": "fixture.pdf",
        }
        alpha = {
            "asset": {
                "name": "alpha",
                "source_file": "alpha.asc",
                "description": "Fixture.",
                "evidence": evidence,
            },
            "columns": [
                {"name": "id", "description": "Identifier.", "evidence": evidence},
                {
                    "name": "value",
                    "description": "Value.",
                    "documented_format": "YYMMDD",
                    "documented_values": {"A": "A value.", "B": "B value."},
                    "evidence": evidence,
                },
            ],
        }
        reference = {
            "asset": {
                "name": "reference",
                "source_file": "reference.asc",
                "description": "Reference fixture.",
                "evidence": evidence,
            },
            "columns": [
                {"name": "id", "description": "Identifier.", "evidence": evidence}
            ],
        }
        (self.catalog / "alpha.yaml").write_text(
            yaml.safe_dump(alpha, sort_keys=False), encoding="utf-8"
        )
        (self.catalog / "reference.yaml").write_text(
            yaml.safe_dump(reference, sort_keys=False), encoding="utf-8"
        )

    def write_relationships(self) -> None:
        relationship = {
            "name": "alpha_value_to_reference_id",
            "from": {"asset": "alpha", "column": "value"},
            "to": {"asset": "reference", "column": "id"},
            "evidence": {
                "type": "source_documentation",
                "document": "fixture.pdf",
            },
        }
        self.relationships.write_text(
            yaml.safe_dump({"relationships": [relationship]}, sort_keys=False),
            encoding="utf-8",
        )

    def write_raw(self, alpha: bytes, reference: bytes) -> None:
        (self.raw / "alpha.asc").write_bytes(alpha)
        (self.raw / "reference.asc").write_bytes(reference)

    def allowed_rule(self, empty_policy: str = "evaluate") -> dict[str, object]:
        return {
            "id": "ALP-VAL-001",
            "asset": "alpha",
            "column": "value",
            "dimension": "validity",
            "expectation": {"type": "allowed_values", "values": ["A", "B"]},
            "empty_policy": empty_policy,
            "evidence": {"type": "catalog", "reference": "alpha.value"},
        }

    def regex_rule(self) -> dict[str, object]:
        return {
            "id": "ALP-VAL-002",
            "asset": "alpha",
            "column": "value",
            "dimension": "validity",
            "expectation": {"type": "regex_format", "pattern": r"^\d{6}$"},
            "empty_policy": "evaluate",
            "evidence": {"type": "catalog", "reference": "alpha.value"},
        }

    def unique_rule(self) -> dict[str, object]:
        return {
            "id": "ALP-UNI-001",
            "asset": "alpha",
            "column": "value",
            "dimension": "uniqueness",
            "expectation": {"type": "unique"},
            "empty_policy": "evaluate",
            "evidence": self.source_evidence(),
        }

    def reference_rule(self) -> dict[str, object]:
        return {
            "id": "ALP-RI-001",
            "asset": "alpha",
            "column": "value",
            "dimension": "referential_integrity",
            "expectation": {
                "type": "reference_exists",
                "reference": {"asset": "reference", "column": "id"},
            },
            "empty_policy": "evaluate",
            "evidence": {
                "type": "catalog_relationship",
                "reference": "alpha_value_to_reference_id",
            },
        }

    def write_rules(self, rules: list[dict[str, object]]) -> None:
        (self.rules / "rules.yaml").write_text(
            yaml.safe_dump({"rules": rules}, sort_keys=False), encoding="utf-8"
        )

    def run_engine(self) -> dict[str, object]:
        return run_dq(
            DQConfig(
                manifest_path=self.manifest,
                catalog_path=self.catalog,
                relationships_path=self.relationships,
                rules_path=self.rules,
                raw_path=self.raw,
                results_path=self.results,
            )
        )

    def test_allowed_values_passes(self) -> None:
        self.write_rules([self.allowed_rule()])
        execution = self.run_engine()
        self.assertEqual(execution["status"], "SUCCESS")
        self.assertEqual(execution["rule_results"][0]["status"], "PASSED")
        self.assertEqual(execution["rule_results"][0]["violations"], 0)

    def test_allowed_values_reports_violation(self) -> None:
        self.write_raw(b"id;value\n1;A\n2;C\n", b"id\nA\nB\n")
        self.write_rules([self.allowed_rule()])
        result = self.run_engine()["rule_results"][0]
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["violations"], 1)
        self.assertEqual(result["sample_violations"], ["C"])

    def test_allowed_values_ignore_excludes_empty_string(self) -> None:
        self.write_raw(b"id;value\n1;A\n2;\n", b"id\nA\nB\n")
        self.write_rules([self.allowed_rule(empty_policy="ignore")])
        result = self.run_engine()["rule_results"][0]
        self.assertEqual(result["rows_total"], 2)
        self.assertEqual(result["rows_evaluated"], 1)
        self.assertEqual(result["violations"], 0)

    def test_regex_format_passes_and_fails(self) -> None:
        self.write_raw(b"id;value\n1;240101\n2;2024-01\n", b"id\nA\n")
        self.write_rules([self.regex_rule()])
        result = self.run_engine()["rule_results"][0]
        self.assertEqual(result["violations"], 1)
        self.assertEqual(result["sample_violations"], ["2024-01"])

    def test_regex_does_not_trim_or_normalize(self) -> None:
        self.write_raw(b"id;value\n1; 240101\n", b"id\nA\n")
        self.write_rules([self.regex_rule()])
        result = self.run_engine()["rule_results"][0]
        self.assertEqual(result["violations"], 1)
        self.assertEqual(result["sample_violations"], [" 240101"])

    def test_unique_passes(self) -> None:
        self.write_rules([self.unique_rule()])
        result = self.run_engine()["rule_results"][0]
        self.assertEqual(result["status"], "PASSED")

    def test_unique_counts_all_rows_in_duplicate_groups(self) -> None:
        self.write_raw(b"id;value\n1;A\n2;A\n3;B\n", b"id\nA\nB\n")
        self.write_rules([self.unique_rule()])
        result = self.run_engine()["rule_results"][0]
        self.assertEqual(result["violations"], 2)
        self.assertEqual(result["sample_violations"], ["A"])

    def test_reference_exists_passes(self) -> None:
        self.write_rules([self.reference_rule()])
        result = self.run_engine()["rule_results"][0]
        self.assertEqual(result["status"], "PASSED")

    def test_reference_exists_reports_violation(self) -> None:
        self.write_raw(b"id;value\n1;A\n2;C\n", b"id\nA\nB\n")
        self.write_rules([self.reference_rule()])
        result = self.run_engine()["rule_results"][0]
        self.assertEqual(result["violations"], 1)
        self.assertEqual(result["sample_violations"], ["C"])

    def test_duplicate_rule_id_fails_metadata_validation(self) -> None:
        rule = self.allowed_rule()
        self.write_rules([rule, dict(rule)])
        execution = self.run_engine()
        self.assertEqual(execution["status"], "FAILED")
        self.assertIn("duplicated", execution["execution_errors"][0]["message"])

    def test_unknown_asset_and_column_fail_metadata_validation(self) -> None:
        unknown_asset = self.allowed_rule()
        unknown_asset["id"] = "UNKNOWN-ASSET"
        unknown_asset["asset"] = "missing"
        unknown_asset["evidence"] = self.source_evidence()
        unknown_column = self.unique_rule()
        unknown_column["id"] = "UNKNOWN-COLUMN"
        unknown_column["column"] = "missing"
        self.write_rules([unknown_asset, unknown_column])
        execution = self.run_engine()
        messages = "\n".join(error["message"] for error in execution["execution_errors"])
        self.assertEqual(execution["status"], "FAILED")
        self.assertIn("Unknown catalog asset", messages)
        self.assertIn("Unknown catalog column", messages)

    def test_unsupported_expectation_type_fails(self) -> None:
        rule = self.allowed_rule()
        rule["expectation"] = {"type": "unsupported"}
        self.write_rules([rule])
        execution = self.run_engine()
        self.assertEqual(execution["status"], "FAILED")
        self.assertIn("Unsupported expectation type", execution["execution_errors"][0]["message"])

    def test_invalid_empty_policy_fails(self) -> None:
        rule = self.allowed_rule(empty_policy="sometimes")
        self.write_rules([rule])
        execution = self.run_engine()
        messages = "\n".join(error["message"] for error in execution["execution_errors"])
        self.assertEqual(execution["status"], "FAILED")
        self.assertIn("Invalid empty_policy", messages)

    def test_execution_succeeds_when_rule_fails(self) -> None:
        self.write_raw(b"id;value\n1;C\n", b"id\nA\nB\n")
        self.write_rules([self.allowed_rule()])
        execution = self.run_engine()
        self.assertEqual(execution["status"], "SUCCESS")
        self.assertEqual(execution["rules_passed"], 0)
        self.assertEqual(execution["rules_failed"], 1)

    def test_raw_files_remain_unchanged(self) -> None:
        self.write_rules([self.allowed_rule(), self.reference_rule()])
        before = {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.raw.glob("*.asc")
        }
        execution = self.run_engine()
        after = {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.raw.glob("*.asc")
        }
        self.assertEqual(execution["status"], "SUCCESS")
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()

