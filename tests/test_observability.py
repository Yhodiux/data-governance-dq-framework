from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import duckdb

from src.observability import ObservabilityConfig, run_observability_build


class ObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.source = self.root / "dq"
        self.database = self.root / "observability" / "dq_history.duckdb"
        self.runs = self.root / "observability" / "runs"
        self.source.mkdir()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def rule_result(
        rule_id: str = "RULE-001",
        status: str = "PASSED",
        samples: list[str] | None = None,
    ) -> dict[str, object]:
        violations = 0 if status == "PASSED" else 1
        return {
            "rule_id": rule_id,
            "asset": "asset",
            "column": "value",
            "dimension": "validity",
            "expectation_type": "allowed_values",
            "empty_policy": "evaluate",
            "status": status,
            "rows_total": 2,
            "rows_evaluated": 2,
            "violations": violations,
            "compliance_ratio": 1.0 if violations == 0 else 0.5,
            "sample_violations": samples if samples is not None else [],
        }

    def run_record(
        self,
        run_id: str = "dq-run-001",
        status: str = "SUCCESS",
        rules: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        if rules is None:
            rules = [self.rule_result()]
        return {
            "run_id": run_id,
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:00:01Z",
            "elapsed_seconds": 1.0,
            "status": status,
            "rules_total": len(rules),
            "rules_passed": sum(rule["status"] == "PASSED" for rule in rules),
            "rules_failed": sum(rule["status"] == "FAILED" for rule in rules),
            "execution_errors": [] if status == "SUCCESS" else [{"message": "failure"}],
            "rule_results": rules,
        }

    def write_record(self, file_name: str, record: object) -> Path:
        path = self.source / file_name
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        return path

    def build(self) -> dict[str, object]:
        return run_observability_build(
            ObservabilityConfig(
                source_path=self.source,
                database_path=self.database,
                runs_path=self.runs,
            )
        )

    def query(self, sql: str) -> list[tuple[object, ...]]:
        connection = duckdb.connect(str(self.database), read_only=True)
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()

    def test_one_valid_run_creates_both_tables(self) -> None:
        self.write_record("run.json", self.run_record())
        execution = self.build()
        self.assertEqual(execution["status"], "SUCCESS")
        tables = {row[0] for row in self.query("SHOW TABLES")}
        self.assertEqual(tables, {"dq_runs", "dq_rule_results"})
        self.assertEqual(self.query("SELECT count(*) FROM dq_runs")[0][0], 1)
        self.assertEqual(self.query("SELECT count(*) FROM dq_rule_results")[0][0], 1)

    def test_multiple_runs_are_loaded(self) -> None:
        self.write_record("a.json", self.run_record("run-a"))
        self.write_record("b.json", self.run_record("run-b"))
        execution = self.build()
        self.assertEqual(execution["dq_runs_loaded"], 2)
        self.assertEqual(execution["dq_rule_results_loaded"], 2)

    def test_repeated_rebuild_does_not_duplicate_rows(self) -> None:
        self.write_record("run.json", self.run_record())
        self.assertEqual(self.build()["status"], "SUCCESS")
        self.assertEqual(self.build()["status"], "SUCCESS")
        self.assertEqual(self.query("SELECT count(*) FROM dq_runs")[0][0], 1)
        self.assertEqual(self.query("SELECT count(*) FROM dq_rule_results")[0][0], 1)

    def test_duplicate_run_id_across_files_fails(self) -> None:
        self.write_record("a.json", self.run_record("duplicate"))
        self.write_record("b.json", self.run_record("duplicate"))
        execution = self.build()
        self.assertEqual(execution["status"], "FAILED")
        self.assertIn("Duplicate run_id", execution["errors"][0]["message"])
        self.assertFalse(self.database.exists())

    def test_duplicate_rule_id_within_run_fails(self) -> None:
        rules = [self.rule_result("same"), self.rule_result("same")]
        self.write_record("run.json", self.run_record(rules=rules))
        execution = self.build()
        self.assertEqual(execution["status"], "FAILED")
        self.assertIn("rule_id must be unique", execution["errors"][0]["message"])

    def test_malformed_json_fails_clearly(self) -> None:
        (self.source / "broken.json").write_text("{not json", encoding="utf-8")
        execution = self.build()
        self.assertEqual(execution["status"], "FAILED")
        self.assertEqual(execution["errors"][0]["source_record"], "broken.json")
        self.assertIn("Cannot read valid JSON", execution["errors"][0]["message"])

    def test_missing_required_run_field_fails(self) -> None:
        record = self.run_record()
        del record["started_at"]
        self.write_record("run.json", record)
        execution = self.build()
        self.assertEqual(execution["status"], "FAILED")
        self.assertIn("started_at", execution["errors"][0]["message"])

    def test_missing_required_rule_result_field_fails(self) -> None:
        record = self.run_record()
        del record["rule_results"][0]["violations"]
        self.write_record("run.json", record)
        execution = self.build()
        self.assertEqual(execution["status"], "FAILED")
        self.assertIn("violations", execution["errors"][0]["message"])

    def test_successful_run_with_inconsistent_rules_total_fails(self) -> None:
        record = self.run_record()
        record["rules_total"] = 2
        self.write_record("run.json", record)
        execution = self.build()
        self.assertEqual(execution["status"], "FAILED")
        self.assertIn("rules_total", execution["errors"][0]["message"])

    def test_successful_run_with_inconsistent_status_counts_fails(self) -> None:
        record = self.run_record()
        record["rules_passed"] = 0
        record["rules_failed"] = 1
        self.write_record("run.json", record)
        execution = self.build()
        self.assertEqual(execution["status"], "FAILED")
        self.assertIn("passed/failed", execution["errors"][0]["message"])

    def test_failed_dq_execution_with_zero_results_is_preserved(self) -> None:
        record = self.run_record("failed-run", status="FAILED", rules=[])
        record["rules_total"] = 20
        self.write_record("failed.json", record)
        execution = self.build()
        self.assertEqual(execution["status"], "SUCCESS")
        self.assertEqual(self.query("SELECT status FROM dq_runs")[0][0], "FAILED")
        self.assertEqual(self.query("SELECT count(*) FROM dq_rule_results")[0][0], 0)

    def test_failed_dq_rule_does_not_fail_observability_build(self) -> None:
        failed_rule = self.rule_result(status="FAILED", samples=["bad"])
        self.write_record("run.json", self.run_record(rules=[failed_rule]))
        execution = self.build()
        self.assertEqual(execution["status"], "SUCCESS")
        self.assertEqual(self.query("SELECT status FROM dq_rule_results")[0][0], "FAILED")

    def test_source_json_files_remain_unchanged(self) -> None:
        path = self.write_record("run.json", self.run_record())
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        self.assertEqual(self.build()["status"], "SUCCESS")
        after = (path.read_bytes(), path.stat().st_mtime_ns)
        self.assertEqual(after, before)

    def test_previous_database_is_preserved_when_rebuild_fails(self) -> None:
        self.write_record("valid.json", self.run_record())
        self.assertEqual(self.build()["status"], "SUCCESS")
        before_hash = hashlib.sha256(self.database.read_bytes()).hexdigest()
        (self.source / "broken.json").write_text("{broken", encoding="utf-8")
        execution = self.build()
        after_hash = hashlib.sha256(self.database.read_bytes()).hexdigest()
        self.assertEqual(execution["status"], "FAILED")
        self.assertEqual(after_hash, before_hash)
        self.assertEqual(self.query("SELECT count(*) FROM dq_runs")[0][0], 1)

    def test_sample_violations_are_preserved_as_json(self) -> None:
        samples = ["bad", "worse"]
        rule = self.rule_result(status="FAILED", samples=samples)
        self.write_record("run.json", self.run_record(rules=[rule]))
        self.assertEqual(self.build()["status"], "SUCCESS")
        stored = self.query("SELECT sample_violations FROM dq_rule_results")[0][0]
        self.assertEqual(json.loads(stored), samples)


if __name__ == "__main__":
    unittest.main()

