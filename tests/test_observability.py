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
        data_zone: str | None = None,
        data_path: str | None = None,
    ) -> dict[str, object]:
        if rules is None:
            rules = [self.rule_result()]
        record = {
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
        if data_zone is not None:
            record["data_zone"] = data_zone
        if data_path is not None:
            record["data_path"] = data_path
        return record

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

    def test_legacy_record_loads_with_null_zone_and_path(self) -> None:
        self.write_record("legacy.json", self.run_record())
        self.assertEqual(self.build()["status"], "SUCCESS")
        self.assertEqual(
            self.query("SELECT data_zone, data_path FROM dq_runs")[0],
            (None, None),
        )

    def test_new_raw_record_preserves_zone_and_path(self) -> None:
        self.write_record(
            "raw.json",
            self.run_record(data_zone="raw", data_path="C:/project/data/raw"),
        )
        self.assertEqual(self.build()["status"], "SUCCESS")
        self.assertEqual(
            self.query("SELECT data_zone, data_path FROM dq_runs")[0],
            ("raw", "C:/project/data/raw"),
        )

    def test_new_trusted_record_preserves_zone_and_path(self) -> None:
        self.write_record(
            "trusted.json",
            self.run_record(data_zone="trusted", data_path="C:/project/data/trusted"),
        )
        self.assertEqual(self.build()["status"], "SUCCESS")
        self.assertEqual(
            self.query("SELECT data_zone, data_path FROM dq_runs")[0],
            ("trusted", "C:/project/data/trusted"),
        )

    def test_raw_and_trusted_runs_coexist_with_correct_rule_association(self) -> None:
        raw_rule = self.rule_result("RULE-RAW", "FAILED", ["raw-value"])
        trusted_rule = self.rule_result("RULE-TRUSTED", "PASSED")
        self.write_record(
            "raw.json",
            self.run_record(
                "run-raw", rules=[raw_rule], data_zone="raw", data_path="/data/raw"
            ),
        )
        self.write_record(
            "trusted.json",
            self.run_record(
                "run-trusted",
                rules=[trusted_rule],
                data_zone="trusted",
                data_path="/data/trusted",
            ),
        )
        self.assertEqual(self.build()["status"], "SUCCESS")
        rows = self.query(
            """
            SELECT r.data_zone, rr.run_id, rr.rule_id, rr.status
            FROM dq_rule_results rr
            JOIN dq_runs r USING (run_id)
            ORDER BY r.data_zone
            """
        )
        self.assertEqual(
            rows,
            [
                ("raw", "run-raw", "RULE-RAW", "FAILED"),
                ("trusted", "run-trusted", "RULE-TRUSTED", "PASSED"),
            ],
        )

    def test_dq_metrics_are_projected_without_recomputation(self) -> None:
        rule = self.rule_result(status="FAILED", samples=["evidence"])
        rule["rows_total"] = 10
        rule["rows_evaluated"] = 9
        rule["violations"] = 7
        rule["compliance_ratio"] = 0.2222222222
        self.write_record("run.json", self.run_record(rules=[rule]))
        self.assertEqual(self.build()["status"], "SUCCESS")
        self.assertEqual(
            self.query(
                "SELECT rows_total, rows_evaluated, violations, compliance_ratio "
                "FROM dq_rule_results"
            )[0],
            (10, 9, 7, 0.2222222222),
        )

    def test_partial_or_invalid_zone_metadata_fails(self) -> None:
        partial = self.run_record()
        partial["data_zone"] = "raw"
        self.write_record("partial.json", partial)
        execution = self.build()
        self.assertEqual(execution["status"], "FAILED")
        self.assertIn("both be present", execution["errors"][0]["message"])

    def test_unsupported_zone_metadata_fails(self) -> None:
        self.write_record(
            "invalid.json",
            self.run_record(data_zone="archive", data_path="/data/archive"),
        )
        execution = self.build()
        self.assertEqual(execution["status"], "FAILED")
        self.assertIn("raw or trusted", execution["errors"][0]["message"])


    def snapshot_record(self, run_id="snapshot-run", cutoff="1995-12-31", **kwargs):
        record = self.run_record(run_id, data_zone="snapshot", data_path="/custom/snapshot", **kwargs)
        record["snapshot_cutoff"] = cutoff
        return record

    def test_snapshot_cutoff_is_date_and_legacy_is_null(self) -> None:
        self.write_record("legacy.json", self.run_record("legacy"))
        self.write_record("snapshot.json", self.snapshot_record())
        self.assertEqual(self.build()["status"], "SUCCESS")
        rows = self.query("SELECT run_id, data_zone, data_path, snapshot_cutoff FROM dq_runs ORDER BY run_id")
        self.assertEqual(rows[0], ("legacy", None, None, None))
        self.assertEqual(str(rows[1][3]), "1995-12-31")
        self.assertEqual(dict((r[0], r[1]) for r in self.query("DESCRIBE dq_runs"))["snapshot_cutoff"], "DATE")

    def test_raw_trusted_null_or_absent_cutoff_preserved(self) -> None:
        for index, (zone, explicit) in enumerate((('raw', False), ('raw', True), ('trusted', False), ('trusted', True))):
            record = self.run_record(str(index), data_zone=zone, data_path="/data")
            if explicit:
                record["snapshot_cutoff"] = None
            self.write_record(f"{index}.json", record)
        self.assertEqual(self.build()["status"], "SUCCESS")
        self.assertEqual(self.query("SELECT count(*) FROM dq_runs WHERE snapshot_cutoff IS NULL")[0][0], 4)

    def test_multiple_cutoffs_preserve_40_historical_results_and_do_not_duplicate(self) -> None:
        historical = [self.rule_result(f"RULE-{i:03}") for i in range(20)]
        self.write_record("legacy.json", self.run_record("legacy", rules=historical))
        self.write_record("trusted.json", self.run_record("trusted", rules=historical, data_zone="trusted", data_path="/trusted"))
        self.assertEqual(self.build()["status"], "SUCCESS")
        before = self.query("SELECT * FROM dq_rule_results ORDER BY run_id, rule_id")
        for year in range(1993, 1999):
            self.write_record(f"{year}.json", self.snapshot_record(str(year), f"{year}-12-31", rules=historical))
        for _ in range(2):
            result = self.build()
            self.assertEqual(result["status"], "SUCCESS")
            self.assertEqual(result["dq_runs_loaded"], 8)
            self.assertEqual(result["dq_rule_results_loaded"], 160)
            self.assertEqual(before, self.query("SELECT * FROM dq_rule_results WHERE run_id IN ('legacy', 'trusted') ORDER BY run_id, rule_id"))
        self.assertEqual(self.query("SELECT count(DISTINCT snapshot_cutoff) FROM dq_runs")[0][0], 6)

    def test_failed_before_evaluation_null_cutoff_or_missing_path_is_preserved(self) -> None:
        for index, cutoff in enumerate((None, "1995-12-31")):
            record = self.snapshot_record(str(index), cutoff, status="FAILED", rules=[])
            record["data_path"] = None
            self.write_record(f"{index}.json", record)
        self.assertEqual(self.build()["status"], "SUCCESS")
        self.assertEqual(self.query("SELECT count(*) FROM dq_runs")[0][0], 2)

    def test_snapshot_invalid_identity_preserves_previous_database(self) -> None:
        self.write_record("valid.json", self.run_record())
        self.assertEqual(self.build()["status"], "SUCCESS")
        original = hashlib.sha256(self.database.read_bytes()).hexdigest()
        for cutoff in (None, "", "1995-2-01", "1995-02-30", 1995):
            self.write_record("invalid.json", self.snapshot_record(cutoff=cutoff))
            self.assertEqual(self.build()["status"], "FAILED")
            self.assertEqual(original, hashlib.sha256(self.database.read_bytes()).hexdigest())

    def test_failed_null_cutoff_after_evaluation_is_rejected(self) -> None:
        record = self.snapshot_record(cutoff=None, status="FAILED")
        self.write_record("invalid.json", record)
        self.assertEqual(self.build()["status"], "FAILED")

    def test_failed_null_cutoff_requires_error_evidence(self) -> None:
        record = self.snapshot_record(cutoff=None, status="FAILED", rules=[])
        record["execution_errors"] = []
        self.write_record("invalid.json", record)
        self.assertEqual(self.build()["status"], "FAILED")

    def test_non_snapshot_cutoff_is_rejected(self) -> None:
        for zone in (None, "raw", "trusted"):
            record = self.run_record(data_zone=zone, data_path="/data" if zone else None)
            record["snapshot_cutoff"] = "1995-12-31"
            self.write_record("invalid.json", record)
            self.assertEqual(self.build()["status"], "FAILED")

    def test_snapshot_failed_after_evaluation_with_valid_cutoff_is_preserved(self) -> None:
        self.write_record("failed.json", self.snapshot_record(status="FAILED"))
        self.assertEqual(self.build()["status"], "SUCCESS")
        self.assertEqual(self.query("SELECT count(*) FROM dq_rule_results")[0][0], 1)


if __name__ == "__main__":
    unittest.main()
