from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

import yaml

from src.standardization import StandardizationConfig, run_standardization


class StandardizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.manifest = self.root / "dataset_manifest.yaml"
        self.catalog = self.root / "catalog"
        self.policies = self.root / "policies"
        self.raw = self.root / "raw"
        self.trusted = self.root / "trusted"
        self.results = self.root / "results" / "standardization"
        self.catalog.mkdir()
        self.policies.mkdir()
        self.raw.mkdir()
        self.trusted.mkdir()
        (self.trusted / ".gitkeep").write_text("", encoding="utf-8")
        self.write_manifest()
        self.write_catalog()
        self.write_policy([self.valid_policy()])
        self.write_raw(
            b'"id";"label";"issued"\n1;"keep";931107 00:00:00\n',
            b'"id";"type";"k_symbol"\n1;"VYBER";" "\n',
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

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
                    for name in ("card.asc", "other.asc")
                ],
            }
        }
        self.manifest.write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )

    def write_catalog(self) -> None:
        evidence = {"type": "source_documentation", "document": "fixture.pdf"}
        card = {
            "asset": {
                "name": "card",
                "source_file": "card.asc",
                "description": "Card fixture.",
                "evidence": evidence,
            },
            "columns": [
                {"name": "id", "description": "ID.", "evidence": evidence},
                {"name": "label", "description": "Label.", "evidence": evidence},
                {
                    "name": "issued",
                    "description": "Issue date.",
                    "documented_format": "YYMMDD",
                    "evidence": evidence,
                },
            ],
        }
        other = {
            "asset": {
                "name": "other",
                "source_file": "other.asc",
                "description": "Other fixture.",
                "evidence": evidence,
            },
            "columns": [
                {"name": name, "description": "Fixture.", "evidence": evidence}
                for name in ("id", "type", "k_symbol")
            ],
        }
        (self.catalog / "card.yaml").write_text(
            yaml.safe_dump(card, sort_keys=False), encoding="utf-8"
        )
        (self.catalog / "other.yaml").write_text(
            yaml.safe_dump(other, sort_keys=False), encoding="utf-8"
        )

    @staticmethod
    def valid_policy() -> dict[str, object]:
        return {
            "id": "CARD-STD-001",
            "asset": "card",
            "column": "issued",
            "transformation": {
                "type": "regex_replace",
                "pattern": r"^(\d{6}) 00:00:00$",
                "replacement": r"\1",
            },
            "evidence": {
                "type": "catalog",
                "reference": "card.issued",
                "description": "Fixture standardization policy.",
            },
        }

    def write_policy(self, policies: list[dict[str, object]]) -> None:
        (self.policies / "card.yaml").write_text(
            yaml.safe_dump({"policies": policies}, sort_keys=False), encoding="utf-8"
        )

    def write_raw(self, card: bytes, other: bytes) -> None:
        (self.raw / "card.asc").write_bytes(card)
        (self.raw / "other.asc").write_bytes(other)

    def build(self) -> dict[str, object]:
        return run_standardization(
            StandardizationConfig(
                manifest_path=self.manifest,
                catalog_path=self.catalog,
                policies_path=self.policies,
                raw_path=self.raw,
                trusted_path=self.trusted,
                results_path=self.results,
            )
        )

    @staticmethod
    def read_rows(path: Path) -> list[list[str]]:
        with path.open("r", encoding="utf-8", newline="") as file:
            return list(csv.reader(file, delimiter=";", quotechar='"'))

    def test_exact_full_match_is_transformed(self) -> None:
        execution = self.build()
        self.assertEqual(execution["status"], "SUCCESS")
        self.assertEqual(self.read_rows(self.trusted / "card.asc")[1][2], "931107")

    def test_nonmatching_value_remains_unchanged(self) -> None:
        self.write_raw(
            b'"id";"label";"issued"\n1;"keep";931107 01:00:00\n',
            (self.raw / "other.asc").read_bytes(),
        )
        execution = self.build()
        self.assertEqual(self.read_rows(self.trusted / "card.asc")[1][2], "931107 01:00:00")
        self.assertEqual(execution["policy_results"][0]["rows_changed"], 0)

    def test_partial_match_is_not_transformed(self) -> None:
        self.write_raw(
            b'"id";"label";"issued"\n1;"keep";x931107 00:00:00\n',
            (self.raw / "other.asc").read_bytes(),
        )
        self.build()
        self.assertEqual(self.read_rows(self.trusted / "card.asc")[1][2], "x931107 00:00:00")

    def test_arbitrary_trailing_text_is_not_removed(self) -> None:
        self.write_raw(
            b'"id";"label";"issued"\n1;"keep";931107 00:00:00 extra\n',
            (self.raw / "other.asc").read_bytes(),
        )
        self.build()
        self.assertEqual(
            self.read_rows(self.trusted / "card.asc")[1][2],
            "931107 00:00:00 extra",
        )

    def test_whitespace_is_not_implicitly_trimmed(self) -> None:
        self.write_raw(
            b'"id";"label";"issued"\n1;"keep"; 931107 00:00:00\n',
            (self.raw / "other.asc").read_bytes(),
        )
        self.build()
        self.assertEqual(
            self.read_rows(self.trusted / "card.asc")[1][2],
            " 931107 00:00:00",
        )

    def test_unaffected_columns_are_preserved(self) -> None:
        before = self.read_rows(self.raw / "card.asc")
        self.build()
        after = self.read_rows(self.trusted / "card.asc")
        self.assertEqual([row[:2] for row in after], [row[:2] for row in before])

    def test_row_order_is_preserved(self) -> None:
        self.write_raw(
            b'"id";"label";"issued"\n3;"c";930103 00:00:00\n1;"a";930101 00:00:00\n2;"b";930102 00:00:00\n',
            (self.raw / "other.asc").read_bytes(),
        )
        self.build()
        rows = self.read_rows(self.trusted / "card.asc")
        self.assertEqual([row[0] for row in rows[1:]], ["3", "1", "2"])

    def test_unaffected_asset_is_byte_identical(self) -> None:
        before = (self.raw / "other.asc").read_bytes()
        self.build()
        self.assertEqual((self.trusted / "other.asc").read_bytes(), before)

    def test_duplicate_policy_ids_fail(self) -> None:
        policy = self.valid_policy()
        self.write_policy([policy, deepcopy(policy)])
        execution = self.build()
        self.assertEqual(execution["status"], "FAILED")
        self.assertIn("Duplicate policy ID", "\n".join(execution["errors"]))

    def test_unknown_asset_fails(self) -> None:
        policy = self.valid_policy()
        policy["asset"] = "missing"
        policy["evidence"]["reference"] = "missing.issued"
        self.write_policy([policy])
        execution = self.build()
        self.assertEqual(execution["status"], "FAILED")
        self.assertIn("unknown catalog asset", "\n".join(execution["errors"]))

    def test_unknown_column_fails(self) -> None:
        policy = self.valid_policy()
        policy["column"] = "missing"
        policy["evidence"]["reference"] = "card.missing"
        self.write_policy([policy])
        execution = self.build()
        self.assertEqual(execution["status"], "FAILED")
        self.assertIn("unknown catalog column", "\n".join(execution["errors"]))

    def test_unsupported_transformation_type_fails(self) -> None:
        policy = self.valid_policy()
        policy["transformation"]["type"] = "trim"
        self.write_policy([policy])
        execution = self.build()
        self.assertEqual(execution["status"], "FAILED")
        self.assertIn("unsupported transformation type", "\n".join(execution["errors"]))

    def test_invalid_regex_fails(self) -> None:
        policy = self.valid_policy()
        policy["transformation"]["pattern"] = "["
        self.write_policy([policy])
        execution = self.build()
        self.assertEqual(execution["status"], "FAILED")
        self.assertIn("invalid regex pattern", "\n".join(execution["errors"]))

    def test_incorrect_catalog_evidence_reference_fails(self) -> None:
        policy = self.valid_policy()
        policy["evidence"]["reference"] = "card.type"
        self.write_policy([policy])
        execution = self.build()
        self.assertEqual(execution["status"], "FAILED")
        self.assertIn("must reference card.issued", "\n".join(execution["errors"]))

    def test_missing_raw_asset_fails(self) -> None:
        (self.raw / "other.asc").unlink()
        execution = self.build()
        self.assertEqual(execution["status"], "FAILED")
        self.assertIn("Required RAW asset is missing", "\n".join(execution["errors"]))

    def test_raw_remains_unchanged(self) -> None:
        before = {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.raw.glob("*.asc")
        }
        self.assertEqual(self.build()["status"], "SUCCESS")
        after = {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.raw.glob("*.asc")
        }
        self.assertEqual(after, before)

    def test_failed_build_preserves_previously_published_trusted(self) -> None:
        self.assertEqual(self.build()["status"], "SUCCESS")
        before = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.trusted.glob("*.asc")
        }
        policy = self.valid_policy()
        policy["transformation"]["pattern"] = "["
        self.write_policy([policy])
        execution = self.build()
        after = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.trusted.glob("*.asc")
        }
        self.assertEqual(execution["status"], "FAILED")
        self.assertEqual(after, before)

    def test_repeated_successful_build_does_not_accumulate_changes(self) -> None:
        self.assertEqual(self.build()["status"], "SUCCESS")
        first = (self.trusted / "card.asc").read_bytes()
        self.assertEqual(self.build()["status"], "SUCCESS")
        second = (self.trusted / "card.asc").read_bytes()
        self.assertEqual(second, first)
        self.assertEqual(len(self.read_rows(self.trusted / "card.asc")), 2)

    def test_sample_changes_are_actual_deterministic_and_capped(self) -> None:
        rows = [
            f'{index};"x";9{index:05d} 00:00:00\n'.encode()
            for index in range(7)
        ]
        self.write_raw(
            b'"id";"label";"issued"\n' + b"".join(rows),
            (self.raw / "other.asc").read_bytes(),
        )
        result = self.build()["policy_results"][0]
        self.assertEqual(result["rows_changed"], 7)
        self.assertEqual(len(result["sample_changes"]), 5)
        self.assertTrue(
            all(change["before"].endswith(" 00:00:00") for change in result["sample_changes"])
        )
        self.assertTrue(
            all(change["after"] == change["before"][:6] for change in result["sample_changes"])
        )

    def test_execution_record_reports_counts_accurately(self) -> None:
        execution = self.build()
        self.assertEqual(execution["policies_total"], 1)
        self.assertEqual(execution["policies_applied"], 1)
        self.assertEqual(execution["assets_total"], 2)
        self.assertEqual(execution["assets_copied"], 1)
        self.assertEqual(execution["assets_transformed"], 1)
        stored = json.loads(Path(execution["execution_record"]).read_text(encoding="utf-8"))
        self.assertEqual(stored["status"], "SUCCESS")
        self.assertEqual(stored["policy_results"][0]["rows_changed"], 1)

    def test_unrelated_vyber_and_whitespace_are_not_modified(self) -> None:
        original = (self.raw / "other.asc").read_bytes()
        self.build()
        trusted = (self.trusted / "other.asc").read_bytes()
        self.assertEqual(trusted, original)
        rows = self.read_rows(self.trusted / "other.asc")
        self.assertEqual(rows[1][1], "VYBER")
        self.assertEqual(rows[1][2], " ")


if __name__ == "__main__":
    unittest.main()

