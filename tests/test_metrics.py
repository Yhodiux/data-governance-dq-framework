"""Synthetic observability inputs; no datasets or pipelines required."""

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import duckdb

from src.metrics.core import MetricsConfig, run_metrics


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = MetricsConfig(self.root)
        self.config.source_path.parent.mkdir(parents=True)
        with duckdb.connect(str(self.config.source_path)) as db:
            db.execute("CREATE TABLE dq_runs(run_id VARCHAR,started_at TIMESTAMP,data_zone VARCHAR,snapshot_cutoff DATE,data_path VARCHAR)")
            db.execute("""INSERT INTO dq_runs VALUES
                ('legacy','2026-01-01',NULL,NULL,NULL),
                ('trusted','2026-01-02','trusted',NULL,'trusted'),
                ('snapshot','2026-01-03','snapshot','1998-12-31','snapshot')""")
            db.execute("""CREATE TABLE dq_rule_results(run_id VARCHAR,rule_id VARCHAR,asset VARCHAR,column_name VARCHAR,
                dimension VARCHAR,expectation_type VARCHAR,empty_policy VARCHAR,status VARCHAR,
                rows_total BIGINT,rows_evaluated BIGINT,violations BIGINT,compliance_ratio DOUBLE)""")
            for run in ("legacy", "trusted", "snapshot"):
                db.execute("INSERT INTO dq_rule_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [run,"CAR-VAL-002","card","issued","validity","regex_format","ignore","FAILED",10,8,2,0.75])
                db.execute("INSERT INTO dq_rule_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [run,"LOA-UNI-001","loan","account_id","uniqueness","unique","evaluate","PASSED",10,10,0,1.0])

    def change(self, sql):
        with duckdb.connect(str(self.config.source_path)) as db:
            db.execute(sql)

    def build(self):
        result = run_metrics(self.config)
        self.assertEqual(result["status"], "SUCCESS", result["errors"])
        return result

    def query(self, sql, args=None):
        with duckdb.connect(str(self.config.database_path), read_only=True) as db:
            return db.execute(sql, args or []).fetchall()

    def test_derived_evaluation_metrics(self):
        self.build()
        self.assertEqual(self.query("""SELECT rows_not_evaluated,evaluation_ratio,conforming_evaluations,violation_ratio,compliance_ratio
            FROM dq_evaluation_metrics WHERE run_id='legacy' AND rule_id='CAR-VAL-002'"""), [(2,0.8,6,0.25,0.75)])

    def test_zero_denominators_preserve_nulls(self):
        self.change("UPDATE dq_rule_results SET rows_total=0,rows_evaluated=0,violations=0,compliance_ratio=NULL WHERE run_id='legacy'")
        self.build()
        self.assertEqual(self.query("SELECT DISTINCT evaluation_ratio,violation_ratio,compliance_ratio FROM dq_evaluation_metrics WHERE run_id='legacy'"), [(None,None,None)])
        self.assertEqual(self.query("SELECT weighted_violation_ratio,weighted_compliance_ratio FROM dq_aggregate_metrics WHERE aggregation_level='RUN' AND run_id='legacy'"), [(None,None)])

    def test_legacy_null_no_raw_inference(self):
        self.build()
        self.assertEqual(self.query("SELECT DISTINCT data_zone,snapshot_cutoff FROM dq_evaluation_metrics WHERE run_id='legacy'"), [(None,None)])
        self.assertEqual(self.query("SELECT count(*) FROM dq_aggregate_metrics WHERE run_id='legacy' AND aggregation_level LIKE 'SNAPSHOT%'"), [(0,)])
        self.assertEqual(self.query("SELECT count(*) FROM dq_evaluation_metrics WHERE data_zone='raw'"), [(0,)])

    def test_all_six_aggregation_levels(self):
        result = self.build()
        self.assertEqual(result["aggregates"],20)
        self.assertEqual(self.query("SELECT aggregation_level,count(*) FROM dq_aggregate_metrics GROUP BY 1 ORDER BY 1"), [("RUN",3),("RUN_ASSET",6),("RUN_DIMENSION",6),("SNAPSHOT",1),("SNAPSHOT_ASSET",2),("SNAPSHOT_DIMENSION",2)])
        self.assertEqual(self.query("SELECT count(*) FROM dq_aggregate_metrics WHERE aggregation_level LIKE 'SNAPSHOT%' AND (run_id IS NOT NULL OR data_zone<>'snapshot' OR snapshot_cutoff IS NULL)"), [(0,)])

    def test_pass_ratio_differs_from_row_rule_compliance(self):
        self.build()
        row = self.query("SELECT evaluations,distinct_rules,passed_evaluations,failed_evaluations,pass_ratio,row_rule_evaluations,row_rule_violations,row_rule_conforming,weighted_compliance_ratio FROM dq_aggregate_metrics WHERE aggregation_level='RUN' AND run_id='legacy'")[0]
        self.assertEqual(row[:8],(2,2,1,1,0.5,18,2,16))
        self.assertAlmostEqual(row[8],16/18)

    def test_row_rule_counts_are_not_unique_rows(self):
        self.change("UPDATE dq_rule_results SET asset='card' WHERE asset='loan'")
        self.build()
        self.assertEqual(self.query("SELECT evaluations,row_rule_evaluations FROM dq_aggregate_metrics WHERE aggregation_level='RUN_ASSET' AND run_id='legacy' AND asset='card'"), [(2,18)])

    def test_snapshots_do_not_combine_different_cutoffs(self):
        self.change("INSERT INTO dq_runs VALUES ('snapshot2','2026-01-04','snapshot','1999-12-31','snapshot2')")
        self.change("INSERT INTO dq_rule_results SELECT 'snapshot2',rule_id,asset,column_name,dimension,expectation_type,empty_policy,status,rows_total,rows_evaluated,violations,compliance_ratio FROM dq_rule_results WHERE run_id='snapshot'")
        self.build()
        self.assertEqual(self.query("SELECT evaluations FROM dq_aggregate_metrics WHERE aggregation_level='SNAPSHOT' ORDER BY snapshot_cutoff"), [(2,),(2,)])

    def test_duplicate_evaluation_rejected(self):
        self.change("INSERT INTO dq_rule_results SELECT * FROM dq_rule_results LIMIT 1")
        self.assertIn("Duplicate",run_metrics(self.config)["errors"][0])

    def test_invalid_counts_fail_closed(self):
        for assignment in ("rows_total=-1","rows_evaluated=-1","violations=-1","rows_evaluated=11","violations=9"):
            with self.subTest(assignment=assignment):
                self.change("UPDATE dq_rule_results SET rows_total=10,rows_evaluated=8,violations=2 WHERE rule_id='CAR-VAL-002'")
                self.change("UPDATE dq_rule_results SET "+assignment+" WHERE rule_id='CAR-VAL-002'")
                self.assertEqual(run_metrics(self.config)["status"],"FAILED")

    def test_invalid_compliance_fail_closed(self):
        for value in ("-0.1","1.1","0.5","NULL","'NaN'::DOUBLE"):
            with self.subTest(value=value):
                self.change("UPDATE dq_rule_results SET compliance_ratio="+value+" WHERE rule_id='CAR-VAL-002'")
                self.assertEqual(run_metrics(self.config)["status"],"FAILED")

    def test_unknown_run_required_fields_and_status(self):
        for assignment in ("run_id='missing'","rule_id=NULL","dimension=NULL","status='UNKNOWN'"):
            with self.subTest(assignment=assignment):
                self.change("UPDATE dq_rule_results SET "+assignment+" WHERE run_id='legacy' AND rule_id='CAR-VAL-002'")
                self.assertEqual(run_metrics(self.config)["status"],"FAILED")
                self.change("DELETE FROM dq_rule_results WHERE run_id='missing' OR rule_id IS NULL OR dimension IS NULL OR status='UNKNOWN'")
                self.change("INSERT INTO dq_rule_results VALUES ('legacy','CAR-VAL-002','card','issued','validity','regex_format','ignore','FAILED',10,8,2,0.75)")

    def test_required_source_column_missing(self):
        self.change("ALTER TABLE dq_rule_results DROP COLUMN empty_policy")
        self.assertEqual(run_metrics(self.config)["status"],"FAILED")

    def test_deterministic_rebuild_and_source_immutability(self):
        before = (hashlib.sha256(self.config.source_path.read_bytes()).hexdigest(),self.config.source_path.stat().st_mtime_ns)
        self.build()
        rows = [self.query("SELECT * FROM "+t+" ORDER BY ALL") for t in ("dq_evaluation_metrics","dq_aggregate_metrics")]
        self.build()
        self.assertEqual(rows,[self.query("SELECT * FROM "+t+" ORDER BY ALL") for t in ("dq_evaluation_metrics","dq_aggregate_metrics")])
        self.assertEqual(before,(hashlib.sha256(self.config.source_path.read_bytes()).hexdigest(),self.config.source_path.stat().st_mtime_ns))

    def test_build_and_publication_failure_preserve_previous(self):
        self.build()
        previous = self.config.database_path.read_bytes()
        with patch("src.metrics.core.os.replace",side_effect=OSError("publication failed")):
            self.assertEqual(run_metrics(self.config)["status"],"FAILED")
        self.assertEqual(previous,self.config.database_path.read_bytes())
        self.change("UPDATE dq_rule_results SET violations=-1")
        self.assertEqual(run_metrics(self.config)["status"],"FAILED")
        self.assertEqual(previous,self.config.database_path.read_bytes())

    def test_cleanup_after_publication_keeps_success(self):
        replace,exists = os.replace,Path.exists
        published=[]
        def wrapped_replace(source,target):
            replace(source,target)
            published.append(Path(source))
        def wrapped_exists(path):
            if path in published:
                raise OSError("cleanup failed")
            return exists(path)
        with patch("src.metrics.core.os.replace",side_effect=wrapped_replace),patch.object(Path,"exists",wrapped_exists):
            self.build()
        self.assertEqual(self.query("SELECT count(*) FROM dq_evaluation_metrics"),[(6,)])

    def test_no_invented_dimensions_or_other_sources(self):
        # Only the synthetic observability DB exists; no traceability or metadata.
        self.build()
        self.assertEqual(self.query("SELECT DISTINCT dimension FROM dq_evaluation_metrics ORDER BY 1"),[("uniqueness",),("validity",)])
        self.assertEqual(self.query("SHOW TABLES"),[("dq_aggregate_metrics",),("dq_evaluation_metrics",)])

    def test_acceptance_queries_a_to_h(self):
        self.build()
        doc=(Path(__file__).resolve().parents[1]/"docs/architecture/data_quality_metrics.md").read_text(encoding="utf-8")
        queries=doc.split("```sql\n")[1].split("```")[0].split(';')[:8]
        rows=[self.query(q,["legacy"] if label in 'ACD' else None) for label,q in zip('ABCDEFGH',queries)]
        self.assertEqual([len(r) for r in rows],[2,3,2,2,1,1,1,2])
        self.assertEqual(rows[6],[("legacy",None,None)])


if __name__ == "__main__":
    unittest.main()
