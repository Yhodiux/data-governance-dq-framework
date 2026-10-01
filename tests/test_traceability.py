"""Synthetic recorded-fact projection tests, independent of local datasets."""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import duckdb

from src.traceability.core import TraceabilityConfig, load_projection, publish_projection, run_traceability


class TraceabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = TraceabilityConfig(self.root)
        common = dict(started_at="2026-01-01T00:00:00Z", completed_at="2026-01-01T00:00:01Z", status="SUCCESS")
        self.records = {
            "ingestion": dict(common, run_id="ingestion-example", source_path="external", raw_path="raw",
                files_expected=1, files_processed=1, files=[dict(file_name="card.asc", validation_status="PASSED",
                    expected_sha256="abc", actual_sha256="abc", expected_record_count=2, actual_record_count=2)]),
            "dq": dict(common, run_id="dq-example", elapsed_seconds=1.0, rules_total=1, rules_passed=0, rules_failed=1,
                execution_errors=[], rule_results=[dict(rule_id="CAR-VAL-002", asset="card", column="issued", status="FAILED",
                    rows_total=2, rows_evaluated=2, violations=1, compliance_ratio=0.5,
                    dimension="validity", expectation_type="regex_format", empty_policy="evaluate", sample_violations=["bad"])]),
            "standardization": dict(common, run_id="std-example", trusted_path="trusted", policies_total=1, policies_applied=1,
                assets_total=1, assets_copied=0, assets_transformed=1, errors=[], policy_results=[dict(policy_id="CARD-STD-001",
                    asset="card", column="issued", rows_total=2, rows_evaluated=2, rows_changed=1, rows_unchanged=1,
                    transformation_type="regex_replace", sample_changes=[dict(before="old", after="new")])]),
            "replay": dict(common, run_id="replay-example", source_zone="trusted", source_path="trusted", snapshot_path="snapshots/1998-12-31",
                cutoff="1998-12-31", metadata={"relationships": [{"name": "not_an_execution_edge"}]}, errors=[],
                assets=[dict(asset="card", strategy="static", input_rows=2, output_rows=2)]),
        }
        for process, record in self.records.items():
            self.write(process, record)

    def write(self, process, record, filename=None):
        path = self.root / "data/results" / process / (filename or record["run_id"] + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def build(self):
        result = run_traceability(self.config)
        self.assertEqual(result["status"], "SUCCESS", result["errors"])
        return result

    def query(self, sql, args=None):
        with duckdb.connect(str(self.config.database_path), read_only=True) as db:
            return db.execute(sql, args or []).fetchall()

    def test_process_types_paths_and_legacy_nulls(self):
        self.build()
        rows = self.query("SELECT process_type,data_zone,snapshot_cutoff,source_path,output_path FROM execution_runs ORDER BY process_type")
        self.assertEqual(rows[0], ("dq", None, None, None, None))
        self.assertEqual(rows[1], ("ingestion", None, None, "external", "raw"))
        self.assertEqual(rows[2][0:2], ("replay", "trusted"))
        self.assertEqual(rows[2][3:], ("trusted", "snapshots/1998-12-31"))
        self.assertEqual(rows[3], ("standardization", None, None, None, "trusted"))
        for path, in self.query("SELECT execution_record_path FROM execution_runs"):
            self.assertFalse(Path(path).is_absolute())
            self.assertTrue((self.root / path).is_file())

    def test_explicit_dq_zone_path_cutoff(self):
        self.records["dq"].update(data_zone="snapshot", data_path="snapshot-input", snapshot_cutoff="1998-12-31")
        self.write("dq", self.records["dq"])
        self.build()
        self.assertEqual(self.query("SELECT data_zone,source_path,CAST(snapshot_cutoff AS VARCHAR) FROM execution_runs WHERE process_type='dq'"), [("snapshot", "snapshot-input", "1998-12-31")])

    def test_only_explicit_metrics(self):
        self.build()
        self.assertEqual(len(self.query("SELECT * FROM execution_metrics")), 10)
        self.assertEqual(self.query("SELECT metric_name,metric_value FROM execution_metrics WHERE run_id='ingestion-example' ORDER BY metric_name"), [("files_expected", 1.0), ("files_processed", 1.0)])
        self.assertEqual(self.query("SELECT * FROM execution_metrics WHERE run_id='replay-example'"), [])
        del self.records["ingestion"]["files_processed"]
        self.write("ingestion", self.records["ingestion"])
        self.assertEqual(self.build()["metrics"], 9)

    def test_four_nested_detail_mappings(self):
        self.build()
        rows = self.query("SELECT detail_type,semantic_id,asset,column_name,status,rows_changed,violations,input_rows,output_rows,attributes FROM execution_details ORDER BY detail_type")
        self.assertEqual(rows[0][:5], ("DQ_RULE", "CAR-VAL-002", "card", "issued", "FAILED"))
        self.assertEqual(rows[0][6], 1)
        self.assertEqual(json.loads(rows[0][-1])["sample_violations"], ["bad"])
        self.assertEqual(rows[1][:5], ("INGESTION_FILE", "card.asc", None, None, "PASSED"))
        self.assertEqual(json.loads(rows[1][-1]), {"expected_sha256": "abc", "actual_sha256": "abc", "expected_record_count": 2, "actual_record_count": 2})
        self.assertEqual(rows[2][7:9], (2, 2))
        self.assertEqual(json.loads(rows[2][-1]), {"strategy": "static"})
        self.assertEqual(rows[3][5], 1)
        self.assertEqual(json.loads(rows[3][-1])["rows_unchanged"], 1)

    def test_deterministic_ids_attributes_and_rebuild(self):
        first = load_projection(self.config)
        self.write("replay", dict(reversed(list(self.records["replay"].items()))))
        self.assertEqual(first, load_projection(self.config))
        self.build()
        before = [self.query(f"SELECT * FROM {table} ORDER BY 1,2") for table in ("execution_runs", "execution_metrics", "execution_details")]
        self.build()
        after = [self.query(f"SELECT * FROM {table} ORDER BY 1,2") for table in ("execution_runs", "execution_metrics", "execution_details")]
        self.assertEqual(before, after)
        self.assertEqual(len({row[0] for row in first[2]}), 4)

    def test_duplicate_run_id_across_processes(self):
        record = dict(self.records["replay"], run_id="dq-example")
        (self.root / "data/results/replay/replay-example.json").unlink()
        self.write("replay", record)
        result = run_traceability(self.config)
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("Duplicate run_id", result["errors"][0])

    def test_filename_mismatch(self):
        self.write("dq", self.records["dq"], "wrong.json")
        self.assertIn("Filename", run_traceability(self.config)["errors"][0])

    def test_invalid_json(self):
        self.write("dq", self.records["dq"]).write_text("{", encoding="utf-8")
        self.assertEqual(run_traceability(self.config)["status"], "FAILED")

    def test_invalid_required_fields_structure_and_process(self):
        for key, value in (("run_id", None), ("status", None), ("rule_results", {}), ("process_type", "unknown")):
            with self.subTest(key=key):
                record = dict(self.records["dq"], **{key: value})
                self.write("dq", record, "dq-example.json")
                self.assertEqual(run_traceability(self.config)["status"], "FAILED")

    def test_invalid_timestamps_and_order(self):
        for start, end in (("bad", "2026-01-01T00:00:01Z"), ("2026-01-01T00:00:00", "2026-01-01T00:00:01Z"), ("2026-01-02T00:00:00Z", "2026-01-01T00:00:01Z")):
            with self.subTest(start=start):
                self.write("dq", dict(self.records["dq"], started_at=start, completed_at=end))
                self.assertEqual(run_traceability(self.config)["status"], "FAILED")

    def test_duplicate_detail_and_metric_protection(self):
        record = copy.deepcopy(self.records["dq"])
        record["rule_results"] *= 2
        self.write("dq", record)
        self.assertIn("Duplicate detail", run_traceability(self.config)["errors"][0])
        path = self.write("dq", self.records["dq"])
        path.write_text(path.read_text().replace('"rules_total": 1', '"rules_total": 1, "rules_total": 2'), encoding="utf-8")
        self.assertIn("Duplicate JSON field", run_traceability(self.config)["errors"][0])
        self.write("dq", self.records["dq"])
        rows = load_projection(self.config)
        with self.assertRaises(duckdb.ConstraintException):
            publish_projection(self.config, (rows[0], rows[1] + [rows[1][0]], rows[2]))

    def test_fk_rejection(self):
        rows = load_projection(self.config)
        bad = [("absent", "files_expected", 1)]
        with self.assertRaises(duckdb.ConstraintException):
            publish_projection(self.config, (rows[0], bad, rows[2]))

    def test_failed_build_preserves_previous_database(self):
        self.build()
        before = self.config.database_path.read_bytes()
        self.write("dq", dict(self.records["dq"], completed_at="invalid"))
        self.assertEqual(run_traceability(self.config)["status"], "FAILED")
        self.assertEqual(before, self.config.database_path.read_bytes())

    def test_publication_failure_preserves_previous_database(self):
        self.build()
        before = self.config.database_path.read_bytes()
        with patch("src.traceability.core.os.replace", side_effect=OSError("replace failed")):
            self.assertEqual(run_traceability(self.config)["status"], "FAILED")
        self.assertEqual(before, self.config.database_path.read_bytes())
        self.assertFalse(list(self.config.database_path.parent.glob("*.staging*")))

    def test_post_replace_cleanup_failure_keeps_success(self):
        original_replace, original_exists = os.replace, Path.exists
        published = []
        def replace(source, target):
            original_replace(source, target)
            published.append(Path(source))
        def exists(path):
            if path in published:
                raise OSError("cleanup failed")
            return original_exists(path)
        with patch("src.traceability.core.os.replace", side_effect=replace), patch.object(Path, "exists", exists):
            self.build()
        self.assertEqual(len(self.query("SELECT * FROM execution_runs")), 4)

    def test_same_replay_cutoff_and_path_remain_separate(self):
        self.write("replay", dict(self.records["replay"], run_id="replay-second"))
        self.build()
        self.assertEqual(self.query("SELECT count(*) FROM execution_runs WHERE process_type='replay'"), [(2,)])
        self.assertEqual(self.query("SELECT count(*) FROM execution_details WHERE detail_type='REPLAY_ASSET'"), [(2,)])

    def test_only_json_inputs_no_causal_relations_or_metadata_projection(self):
        original_read = Path.read_text
        def read(path, *args, **kwargs):
            self.assertIn(path.parent.name, self.records)
            self.assertEqual(path.suffix, ".json")
            return original_read(path, *args, **kwargs)
        with patch.object(Path, "read_text", read):
            self.build()
        self.assertEqual(self.query("SHOW TABLES"), [("execution_details",), ("execution_metrics",), ("execution_runs",)])
        self.assertNotIn("not_an_execution_edge", str(self.query("SELECT attributes FROM execution_details")))
        columns = self.query("SELECT column_name FROM information_schema.columns")
        self.assertFalse(any("upstream" in c or "downstream" in c or "parent_run" in c for c, in columns))

    def test_acceptance_queries_a_to_g(self):
        self.write("replay", dict(self.records["replay"], run_id="replay-second"))
        self.build()
        document = (Path(__file__).resolve().parents[1] / "docs/architecture/execution_traceability.md").read_text(encoding="utf-8")
        queries = document.split("```sql\n")[1].split("```")[0].split(";")[:7]
        results = [self.query(sql, ["dq-example"] if i == 2 else None) for i, sql in enumerate(queries)]
        self.assertEqual([len(rows) for rows in results], [5, 1, 1, 1, 2, 2, 1])
        self.assertEqual(results[2][0][0], "CAR-VAL-002")
        self.assertEqual(results[3][0][-1], 1)


if __name__ == "__main__":
    unittest.main()
