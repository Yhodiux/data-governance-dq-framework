"""Synthetic result databases; original pipelines are never invoked."""

from datetime import datetime, date
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import duckdb

from src.reporting.core import ReportingConfig, SCHEMAS, SOURCES, run_reporting


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = ReportingConfig(self.root)
        self.time = datetime(2026, 1, 1)
        self.cutoff = date(1993, 12, 31)
        for source in SOURCES.values():
            (self.root/'data/results'/source).parent.mkdir(parents=True, exist_ok=True)
        self.make_table('metrics', 'dq_evaluation_metrics', 'dq_evaluations', [dict(run_id='dq1', rule_id='R1', started_at=self.time, data_zone='snapshot', snapshot_cutoff=self.cutoff, asset='card', column_name='issued', dimension='validity', expectation_type='regex_format', empty_policy='ignore', status='FAILED', rows_total=3, rows_evaluated=2, rows_not_evaluated=1, evaluation_ratio=2/3, violations=1, conforming_evaluations=1, violation_ratio=.5, compliance_ratio=.5)])
        summaries = []
        for level,asset,dim in [('RUN',None,None), ('RUN_ASSET','card',None), ('RUN_DIMENSION',None,'validity'), ('SNAPSHOT',None,None)]:
            summaries.append(dict(aggregation_level=level,run_id='dq1' if level!='SNAPSHOT' else None,data_zone='snapshot',snapshot_cutoff=self.cutoff,asset=asset,dimension=dim,evaluations=1,distinct_rules=1,passed_evaluations=0,failed_evaluations=1,pass_ratio=0.,row_rule_evaluations=2,row_rule_violations=1,row_rule_conforming=1,weighted_violation_ratio=.5,weighted_compliance_ratio=.5))
        self.make_table('metrics','dq_aggregate_metrics','dq_summaries',summaries,omit={'summary_id'},huge=True)
        self.make_table('governance','governance_issues','governance_issues',[dict(issue_id='I1',asset='card',column_name='issued',rule_id='R1',status='OPEN',description='Finding')])
        self.make_table('governance','governance_decisions','governance_decisions',[
            dict(decision_id='D1',issue_id='I1',decision_type='REMEDIATION_AUTHORIZED',status='EFFECTIVE',policy_id='P1',rationale='Authorized'),
            dict(decision_id='D2',issue_id='I1',decision_type='REMEDIATION_WITHHELD',status='ACTIVE',rationale='Withheld')],omit={'policy_asset','policy_column'})
        self.make_table('traceability','execution_runs','execution_runs',[dict(run_id='dq1',process_type='dq',status='SUCCESS',started_at=self.time,completed_at=self.time,data_zone='snapshot',snapshot_cutoff=self.cutoff,execution_record_path='dq1.json')])
        self.make_table('traceability','execution_details','execution_details',[dict(detail_id='detail1',run_id='dq1',detail_type='DQ_RULE',semantic_id='R1',asset='card',column_name='issued',status='FAILED',attributes='{"sample_violations":[" "]}')],json_attributes=True)
        with self.connect('lineage') as c:
            c.execute('CREATE TABLE lineage_nodes(node_id VARCHAR,node_type VARCHAR,zone VARCHAR,asset VARCHAR,column_name VARCHAR,semantic_id VARCHAR)')
            c.executemany('INSERT INTO lineage_nodes VALUES (?,?,?,?,?,?)', [('a1','asset','source','card',None,None), ('a2','asset','raw','card',None,None), ('r1','dq_rule',None,'card','issued','R1'), ('p1','standardization_policy',None,'card','issued','P1')])

    def connect(self, name):
        return duckdb.connect(str(self.root/'data/results'/SOURCES[name]))

    def make_table(self, source, table, dataset, rows, omit=None, huge=False, json_attributes=False):
        fields = [(col, 'HUGEINT' if huge and col.startswith('row_rule_') else 'JSON' if json_attributes and col=='attributes' else typ) for col,typ in SCHEMAS[dataset] if col not in (omit or set())]
        with self.connect(source) as c:
            c.execute(f"CREATE TABLE {table} ({','.join(col+' '+typ for col,typ in fields)})")
            c.executemany(f"INSERT INTO {table} VALUES ({','.join('?' for _ in fields)})", [[r.get(col) for col,_ in fields] for r in rows])

    def build(self):
        result = run_reporting(self.config)
        self.assertEqual(result['status'],'SUCCESS',result['errors'])
        return result

    def logical(self):
        with duckdb.connect(':memory:') as c:
            return {p.name:c.execute('SELECT * FROM read_parquet(?) ORDER BY ALL',[str(p)]).fetchall() for p in sorted(self.config.output_path.glob('*.parquet'))}

    def reject(self, message):
        result = run_reporting(self.config)
        self.assertEqual(result['status'],'FAILED')
        self.assertIn(message,str(result['errors']))

    def test_eight_schemas_nulls_types_and_existing_values(self):
        result = self.build()
        self.assertEqual(len(list(self.config.output_path.iterdir())),8)
        self.assertEqual(result['counts']['dq_summaries'],3)
        with duckdb.connect(':memory:') as c:
            for name,fields in SCHEMAS.items():
                actual = c.execute('DESCRIBE SELECT * FROM read_parquet(?)',[str(self.config.output_path/f'{name}.parquet')]).fetchall()
                self.assertEqual([(r[0],r[1]) for r in actual],list(fields))
            rows = c.execute('SELECT actor,decision_date,policy_asset,policy_column FROM read_parquet(?) ORDER BY decision_id',[str(self.config.output_path/'governance_decisions.parquet')]).fetchall()
            self.assertEqual(rows,[(None,None,'card','issued'),(None,None,None,None)])
            row = c.execute('SELECT started_at,snapshot_cutoff,rows_evaluated,violations,compliance_ratio FROM read_parquet(?)',[str(self.config.output_path/'dq_evaluations.parquet')]).fetchone()
            self.assertEqual(row,(self.time,self.cutoff,2,1,.5))
            attr = c.execute('SELECT attributes FROM read_parquet(?)',[str(self.config.output_path/'execution_details.parquet')]).fetchone()[0]
            self.assertEqual(attr,'{"sample_violations":[" "]}')

    def test_determinism_and_read_only_inputs(self):
        paths = [self.root/'data/results'/p for p in SOURCES.values()]
        before = [(p.read_bytes(),p.stat().st_mtime_ns) for p in paths]
        self.build()
        first = self.logical()
        self.build()
        self.assertEqual(first,self.logical())
        self.assertEqual(before,[(p.read_bytes(),p.stat().st_mtime_ns) for p in paths])

    def test_duplicate_grains(self):
        for source,table in [('metrics','dq_evaluation_metrics'),('metrics','dq_aggregate_metrics'),('governance','governance_issues'),('traceability','execution_runs')]:
            with self.subTest(table=table):
                with self.connect(source) as c:
                    c.execute(f'INSERT INTO {table} SELECT * FROM {table} LIMIT 1')
                self.reject('Duplicate grain')
                with self.connect(source) as c:
                    c.execute(f'DELETE FROM {table} WHERE rowid=(SELECT max(rowid) FROM {table})')

    def test_fk_and_scope_validation(self):
        for source,table,col,value,message in [
            ('metrics','dq_evaluation_metrics','run_id','missing','Invalid FK'),
            ('governance','governance_decisions','issue_id','missing','Invalid FK'),
            ('metrics','dq_evaluation_metrics','rule_id','missing','Invalid FK'),
            ('metrics','dq_evaluation_metrics','column_name','wrong','Rule scope mismatch'),
            ('governance','governance_decisions','policy_id','missing','policy scope')]:
            with self.subTest(table=table,col=col):
                with self.connect(source) as c:
                    old = c.execute(f'SELECT {col} FROM {table} ORDER BY 1 NULLS LAST LIMIT 1').fetchone()[0]
                    c.execute(f'UPDATE {table} SET {col}=? WHERE {col}=?',[value,old])
                self.reject(message)
                with self.connect(source) as c:
                    c.execute(f'UPDATE {table} SET {col}=? WHERE {col}=?',[old,value])

    def test_schema_drift_and_missing_input_fail_closed(self):
        with self.connect('metrics') as c:
            c.execute('ALTER TABLE dq_evaluation_metrics ADD COLUMN unexpected VARCHAR')
        self.reject('Unexpected source schema')
        path = self.root/'data/results'/SOURCES['metrics']
        path.unlink()
        self.reject('Missing source')

    def test_run_identity_is_not_inferred(self):
        with self.connect('metrics') as c:
            c.execute("UPDATE dq_evaluation_metrics SET data_zone=NULL")
        self.reject('DQ run identity mismatch')

    def test_legacy_nulls_zero_denominators_and_repeated_cutoffs(self):
        with self.connect('metrics') as c:
            c.execute("INSERT INTO dq_evaluation_metrics SELECT * REPLACE('dq2' AS run_id) FROM dq_evaluation_metrics")
        with self.connect('traceability') as c:
            c.execute("INSERT INTO execution_runs SELECT * REPLACE('dq2' AS run_id) FROM execution_runs")
        self.assertEqual(self.build()['counts']['dq_evaluations'],2)
        self.assertTrue(all(r[4] == self.cutoff for r in self.logical()['dq_evaluations.parquet']))
        with self.connect('metrics') as c:
            c.execute('UPDATE dq_evaluation_metrics SET data_zone=NULL,snapshot_cutoff=NULL,rows_evaluated=0,violations=0,compliance_ratio=NULL,violation_ratio=NULL')
            c.execute('UPDATE dq_aggregate_metrics SET data_zone=NULL,snapshot_cutoff=NULL')
        with self.connect('traceability') as c:
            c.execute('UPDATE execution_runs SET data_zone=NULL,snapshot_cutoff=NULL')
        self.assertEqual(self.build()['counts']['dq_evaluations'],2)
        self.assertTrue(all(r[3] is None and r[4] is None for r in self.logical()['dq_evaluations.parquet']))

    def test_hugeint_roundtrip_is_exact(self):
        value = 2**70+1
        with self.connect('metrics') as c:
            c.execute('UPDATE dq_aggregate_metrics SET row_rule_evaluations=?',[value])
        self.build()
        with duckdb.connect(':memory:') as c:
            values = c.execute('SELECT row_rule_evaluations FROM read_parquet(?)',[str(self.config.output_path/'dq_summaries.parquet')]).fetchall()
        self.assertEqual([int(r[0]) for r in values],[value]*3)

    def test_export_failure_preserves_publication(self):
        self.build()
        before = self.logical()
        with patch('src.reporting.core.project',side_effect=ValueError('projection failed')):
            self.reject('projection failed')
        self.assertEqual(before,self.logical())

    def test_swap_failure_rolls_back_whole_directory(self):
        self.build()
        before = self.logical()
        original = os.replace
        def replace(source,target):
            if '.reporting-staging-' in str(source):
                raise OSError('publish failed')
            return original(source,target)
        with patch('src.reporting.core.os.replace',side_effect=replace):
            self.reject('publish failed')
        self.assertEqual(before,self.logical())
        self.assertFalse(list(self.config.output_path.parent.glob('.reporting-*')))

    def test_post_publication_cleanup_is_best_effort(self):
        self.build()
        with patch('src.reporting.core.cleanup',side_effect=OSError('cleanup failed')):
            self.build()
        self.assertEqual(len(self.logical()),8)

    def test_rollback_failure_retains_previous_backup(self):
        self.build()
        original = os.replace
        def replace(source,target):
            if '.reporting-staging-' in str(source) or '.reporting-backup-' in str(source):
                raise OSError('swap unavailable')
            return original(source,target)
        with patch('src.reporting.core.os.replace',side_effect=replace):
            self.reject('swap unavailable')
        backups = list(self.config.output_path.parent.glob('.reporting-backup-*'))
        self.assertEqual(len(backups),1)
        self.assertEqual(len(list(backups[0].glob('*.parquet'))),8)
