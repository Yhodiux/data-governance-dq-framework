from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import yaml

from src.dq import DQConfig, run_dq
from src.dq.__main__ import build_parser


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

    def run_engine(
        self, data_path: Path | None = None, data_zone: str = "raw",
        snapshot_cutoff: str | None = None,
    ) -> dict[str, object]:
        return run_dq(
            DQConfig(
                manifest_path=self.manifest,
                catalog_path=self.catalog,
                relationships_path=self.relationships,
                rules_path=self.rules,
                data_path=data_path or self.raw,
                data_zone=data_zone,
                results_path=self.results,
                snapshot_cutoff=snapshot_cutoff,
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

    def test_cli_defaults_to_raw_zone_and_path(self) -> None:
        args = build_parser().parse_args([])
        self.assertEqual(args.data_zone, "raw")
        self.assertEqual(args.data_path, Path("data/raw"))

    def test_explicit_trusted_path_can_be_validated(self) -> None:
        trusted = self.root / "trusted"
        trusted.mkdir()
        (trusted / "alpha.asc").write_bytes((self.raw / "alpha.asc").read_bytes())
        (trusted / "reference.asc").write_bytes(
            (self.raw / "reference.asc").read_bytes()
        )
        self.write_rules([self.allowed_rule()])
        execution = self.run_engine(trusted, "trusted")
        self.assertEqual(execution["status"], "SUCCESS")
        self.assertEqual(execution["data_zone"], "trusted")
        self.assertEqual(execution["data_path"], str(trusted.resolve()))

    def test_execution_record_contains_zone_and_resolved_path(self) -> None:
        self.write_rules([self.allowed_rule()])
        execution = self.run_engine()
        stored = json.loads(
            Path(execution["execution_record"]).read_text(encoding="utf-8")
        )
        self.assertEqual(stored["data_zone"], "raw")
        self.assertEqual(stored["data_path"], str(self.raw.resolve()))

    def test_invalid_data_zone_is_rejected(self) -> None:
        self.write_rules([self.allowed_rule()])
        execution = self.run_engine(data_zone="archive")
        self.assertEqual(execution["status"], "FAILED")
        self.assertIn("Unsupported data_zone", execution["execution_errors"][0]["message"])

    def test_data_path_change_does_not_change_rules_or_semantics(self) -> None:
        trusted = self.root / "trusted"
        trusted.mkdir()
        for path in self.raw.glob("*.asc"):
            (trusted / path.name).write_bytes(path.read_bytes())
        self.write_rules([self.allowed_rule(), self.reference_rule()])
        raw_execution = self.run_engine(self.raw, "raw")
        trusted_execution = self.run_engine(trusted, "trusted")
        self.assertEqual(raw_execution["rule_results"], trusted_execution["rule_results"])
        self.assertEqual(raw_execution["rules_total"], trusted_execution["rules_total"])

    def test_dq_does_not_transform_trusted_input(self) -> None:
        trusted = self.root / "trusted"
        trusted.mkdir()
        (trusted / "alpha.asc").write_bytes(b"id;value\n1;C\n")
        (trusted / "reference.asc").write_bytes(b"id\nA\nB\n")
        self.write_rules([self.allowed_rule()])
        before = {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in trusted.glob("*.asc")
        }
        execution = self.run_engine(trusted, "trusted")
        after = {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in trusted.glob("*.asc")
        }
        self.assertEqual(execution["status"], "SUCCESS")
        self.assertEqual(execution["rule_results"][0]["status"], "FAILED")
        self.assertEqual(after, before)


    def test_snapshot_valid_identity_and_no_directory_inference(self) -> None:
        snapshot = self.root / "1993-12-31"
        snapshot.mkdir()
        for path in self.raw.glob("*.asc"):
            (snapshot / path.name).write_bytes(path.read_bytes())
        self.write_rules([self.allowed_rule()])
        execution = self.run_engine(snapshot, "snapshot", "1995-12-31")
        self.assertEqual(execution["status"], "SUCCESS")
        self.assertEqual(execution["snapshot_cutoff"], "1995-12-31")
        stored = json.loads(Path(execution["execution_record"]).read_text())
        self.assertEqual(stored["snapshot_cutoff"], "1995-12-31")

    def test_invalid_snapshot_cutoffs_fail_before_evaluation(self) -> None:
        self.write_rules([self.allowed_rule()])
        for cutoff in (None, "", "1995-2-01", "1995-02-30", "not-date", "1995-12-31T00:00:00", 1995):
            with self.subTest(cutoff=cutoff), patch("src.dq.core.execute_rule") as operator:
                execution = self.run_engine(self.raw, "snapshot", cutoff)
                self.assertEqual(execution["status"], "FAILED")
                self.assertIsNone(execution["snapshot_cutoff"])
                self.assertEqual(execution["rules_total"], 0)
                self.assertEqual(execution["rule_results"], [])
                self.assertIn(repr(cutoff), execution["execution_errors"][0]["message"])
                operator.assert_not_called()

    def test_raw_trusted_reject_any_supplied_cutoff(self) -> None:
        self.write_rules([self.allowed_rule()])
        for zone in ("raw", "trusted"):
            for cutoff in ("1995-12-31", ""):
                with self.subTest(zone=zone, cutoff=cutoff), patch("src.dq.core.execute_rule") as operator:
                    execution = self.run_engine(self.raw, zone, cutoff)
                    self.assertEqual(execution["status"], "FAILED")
                    self.assertIsNone(execution["snapshot_cutoff"])
                    self.assertEqual(execution["rule_results"], [])
                    operator.assert_not_called()

    def test_raw_trusted_new_records_have_null_cutoff(self) -> None:
        self.write_rules([self.allowed_rule()])
        for zone in ("raw", "trusted"):
            execution = self.run_engine(self.raw, zone)
            self.assertEqual(execution["status"], "SUCCESS")
            stored = json.loads(Path(execution["execution_record"]).read_text())
            self.assertIn("snapshot_cutoff", stored)
            self.assertIsNone(stored["snapshot_cutoff"])

    def test_same_content_all_zones_has_identical_rule_results(self) -> None:
        self.write_raw(b'id;value\n1;A\n2;A\n3;VYBER\n4;" "\n5;\n', b'id\nA\nB\n')
        self.write_rules([self.allowed_rule("ignore"), self.regex_rule(), self.unique_rule(), self.reference_rule()])
        raw = self.run_engine(self.raw, "raw")
        trusted = self.run_engine(self.raw, "trusted")
        snapshot = self.run_engine(self.raw, "snapshot", "1995-12-31")
        self.assertEqual(snapshot["status"], "SUCCESS")
        self.assertEqual(raw["rule_results"], trusted["rule_results"])
        self.assertEqual(raw["rule_results"], snapshot["rule_results"])

    def test_snapshot_preserves_whitespace_vyber_and_input(self) -> None:
        self.write_raw(b'id;value\r\n1;VYBER\r\n2;" "\r\n', b'id\r\nA\r\n')
        self.write_rules([self.allowed_rule("ignore")])
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.raw.glob("*.asc")}
        execution = self.run_engine(self.raw, "snapshot", "1995-12-31")
        self.assertEqual(execution["status"], "SUCCESS")
        rule = execution["rule_results"][0]
        self.assertEqual(rule["violations"], 2)
        self.assertEqual(rule["sample_violations"], [" ", "VYBER"])
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.raw.glob("*.asc")})

    def test_snapshot_header_only_preserves_semantics(self) -> None:
        self.write_raw(b'id;value\n', b'id\n')
        self.write_rules([self.allowed_rule(), self.regex_rule(), self.unique_rule(), self.reference_rule()])
        execution = self.run_engine(self.raw, "snapshot", "1990-01-01")
        self.assertEqual(execution["status"], "SUCCESS")
        for rule in execution["rule_results"]:
            self.assertEqual(rule["status"], "PASSED")
            self.assertEqual(rule["rows_evaluated"], 0)
            self.assertEqual(rule["violations"], 0)
            self.assertIsNone(rule["compliance_ratio"])
            self.assertEqual(rule["sample_violations"], [])

    def test_snapshot_cutoff_does_not_filter_rows(self) -> None:
        self.write_raw(b'id;value\n1;991231\n', b'id\n')
        self.write_rules([self.regex_rule()])
        execution = self.run_engine(self.raw, "snapshot", "1990-01-01")
        self.assertEqual(execution["status"], "SUCCESS")
        self.assertEqual(execution["rule_results"][0]["rows_evaluated"], 1)

    def test_snapshot_cli_explicit_path_tracking(self) -> None:
        parser = build_parser()
        default = parser.parse_args([])
        self.assertEqual(default.data_path, Path("data/raw"))
        self.assertFalse(default.data_path_explicit)
        missing = parser.parse_args(["--data-zone", "snapshot", "--snapshot-cutoff", "1995-12-31"])
        self.assertFalse(missing.data_path_explicit)
        explicit = parser.parse_args(["--data-zone", "snapshot", "--data-path", "custom", "--snapshot-cutoff", "1995-12-31"])
        self.assertTrue(explicit.data_path_explicit)
        self.assertEqual(explicit.snapshot_cutoff, "1995-12-31")

    def test_snapshot_cli_missing_path_records_failed_execution(self) -> None:
        from src.dq.__main__ import main
        from src.dq.core import run_dq as real_run
        observed = []

        def fixture_run(config):
            from dataclasses import replace
            config = replace(config, results_path=self.results)
            observed.append(real_run(config))
            return observed[-1]

        with patch("sys.argv", ["dq", "--data-zone", "snapshot", "--snapshot-cutoff", "1995-12-31"]), patch("src.dq.__main__.run_dq", side_effect=fixture_run), patch("src.dq.__main__.logging.basicConfig"):
            self.assertEqual(main(), 1)
        self.assertEqual(observed[0]["status"], "FAILED")
        self.assertIsNone(observed[0]["data_path"])
        self.assertEqual(observed[0]["rule_results"], [])
        from src.observability import ObservabilityConfig, run_observability_build
        result = run_observability_build(ObservabilityConfig(
            self.results, self.root / "history.duckdb", self.root / "obs-runs"
        ))
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["dq_rule_results_loaded"], 0)

    def test_failed_cutoff_record_is_ingestible_by_observability(self) -> None:
        from src.observability import ObservabilityConfig, run_observability_build
        execution = self.run_engine(self.raw, "snapshot", "1995-02-30")
        self.assertEqual(execution["status"], "FAILED")
        result = run_observability_build(ObservabilityConfig(
            self.results, self.root / "history.duckdb", self.root / "obs-runs"
        ))
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["dq_runs_loaded"], 1)
        self.assertEqual(result["dq_rule_results_loaded"], 0)


    def test_raw_none_data_path_fails(self) -> None:
        execution = run_dq(DQConfig(
            manifest_path=self.manifest,
            catalog_path=self.catalog,
            relationships_path=self.relationships,
            rules_path=self.rules,
            data_path=None,
            data_zone="raw",
            results_path=self.results,
        ))
        self.assertEqual(execution["status"], "FAILED")
        self.assertEqual(execution["rule_results"], [])
        self.assertIn("data_path cannot be None", execution["execution_errors"][0]["message"])


if __name__ == "__main__":
    unittest.main()
