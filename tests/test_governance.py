"""Isolated governance contracts; no dataset or pipeline execution."""

import copy
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import duckdb
import yaml

from src.governance.core import GovernanceConfig, load_registry, run_governance


class GovernanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = GovernanceConfig(self.root)
        scopes = [('card', 'issued', 'CAR-VAL-002'), ('trans', 'type', 'TRA-VAL-002'),
                  ('order', 'k_symbol', 'ORD-VAL-001'), ('trans', 'k_symbol', 'TRA-VAL-004')]
        self.write('metadata/dq_rules/validity.yaml', {'rules': [dict(id=r, asset=a, column=c) for a,c,r in scopes]})
        self.write('metadata/standardization/card.yaml', {'policies': [dict(id='CARD-STD-001', asset='card', column='issued')]})
        for a in {'card', 'trans', 'order'}:
            self.write(f'metadata/catalog/{a}.yaml', {'asset': {'name': a}, 'columns': [{'name': c} for asset,c,_ in scopes if asset==a]})
        self.issues = []
        self.decisions = []
        for n, (a,c,r) in enumerate(scopes, 1):
            self.issues.append(dict(issue_id=f'GOV-ISS-{n:03}', asset=a, column=c, rule_id=r,
                status='RESOLVED' if n==1 else 'OPEN', description='Recorded finding', evidence=[]))
            self.decisions.append(dict(decision_id=f'GOV-DEC-{n:03}', issue_id=f'GOV-ISS-{n:03}',
                decision_type='REMEDIATION_AUTHORIZED' if n==1 else 'REMEDIATION_WITHHELD',
                status='EFFECTIVE' if n==1 else 'ACTIVE', policy_id='CARD-STD-001' if n==1 else None,
                rationale='Explicit project decision'))
        self.write('data/results/dq/dq-run.json', {'run_id': 'dq-run', 'rule_results': [dict(rule_id='CAR-VAL-002', asset='card', column='issued')]})
        self.write('data/results/standardization/std-run.json', {'run_id': 'std-run', 'policy_results': [dict(policy_id='CARD-STD-001', asset='card', column='issued')]})
        self.issues[0]['evidence'] = [
            dict(type='DQ_EXECUTION', path='data/results/dq/dq-run.json', run_id='dq-run', rule_id='CAR-VAL-002', description='DQ fact'),
            dict(type='STANDARDIZATION_EXECUTION', path='data/results/standardization/std-run.json', run_id='std-run', policy_id='CARD-STD-001', description='Standardization fact'),
            dict(type='METADATA', path='metadata/catalog/card.yaml', description='Catalog scope')]
        self.save()

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value) if path.suffix=='.json' else yaml.safe_dump(value), encoding='utf-8')

    def save(self):
        self.write('metadata/governance/issues.yaml', {'issues': self.issues})
        self.write('metadata/governance/decisions.yaml', {'decisions': self.decisions})

    def reject(self, contains):
        self.save()
        result = run_governance(self.config)
        self.assertEqual(result['status'], 'FAILED')
        self.assertIn(contains, str(result['errors']))

    def test_duplicate_ids_and_issue_fk(self):
        for target, message in [('issues', 'Duplicate issue_id'), ('decisions', 'Duplicate decision_id')]:
            with self.subTest(target=target):
                values = getattr(self, target)
                values.append(copy.deepcopy(values[0]))
                self.reject(message)
                values.pop()
        self.decisions[0]['issue_id'] = 'missing'
        self.reject('unknown issue')

    def test_rule_existence_and_scope(self):
        self.issues[0]['rule_id'] = 'missing'
        self.reject('Unknown rule')
        self.issues[0]['rule_id'] = 'CAR-VAL-002'
        self.issues[0]['column'] = 'other'
        self.reject('Rule scope')

    def test_policy_existence_scope_and_authorization_requirement(self):
        for value, error in [(None, 'requires policy_id'), ('missing', 'Unknown policy')]:
            self.decisions[0]['policy_id'] = value
            self.reject(error)
        self.decisions[0]['policy_id'] = 'CARD-STD-001'
        self.write('metadata/standardization/card.yaml', {'policies': [dict(id='CARD-STD-001', asset='trans', column='type')]})
        self.reject('Policy scope')

    def test_evidence_paths_and_projection_rejection(self):
        for path in ['missing.json', '../outside.json', 'data/results/metrics/dq_metrics.duckdb']:
            with self.subTest(path=path):
                self.issues[0]['evidence'][0]['path'] = path
                if path.startswith('data/'):
                    p = self.root / path
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text('not execution evidence')
                self.reject('Evidence path' if not path.startswith('data/') else 'original process JSON')

    def test_execution_identity_and_scope(self):
        original = copy.deepcopy(self.issues[0]['evidence'])
        for index, key, value, message in [(0,'run_id','wrong','run identity'),
                (1,'run_id','wrong','run identity'), (0,'rule_id','TRA-VAL-002','rule conflicts')]:
            with self.subTest(index=index, key=key):
                self.issues[0]['evidence'] = copy.deepcopy(original)
                self.issues[0]['evidence'][index][key] = value
                self.reject(message)
        self.issues[0]['evidence'] = original
        for process, detail, semantic in [('dq','rule_results','rule_id'), ('standardization','policy_results','policy_id')]:
            name = 'dq-run' if process=='dq' else 'std-run'
            identifier = 'CAR-VAL-002' if process=='dq' else 'CARD-STD-001'
            for changed in [dict(), dict(asset='trans', column='type')]:
                record = {semantic: identifier, 'asset': 'card', 'column': 'issued'}
                record.update(changed)
                if not changed:
                    record[semantic] = 'wrong'
                self.write(f'data/results/{process}/{name}.json', {'run_id': name, detail: [record]})
                self.reject('semantic identity/scope')
            self.write(f'data/results/{process}/{name}.json', {'run_id': name, detail: [{semantic: identifier, 'asset':'card','column':'issued'}]})

    def test_metadata_selectors_and_catalog_scope(self):
        self.issues[0]['evidence'] = [dict(type='METADATA', path='metadata/dq_rules/validity.yaml', rule_id='CAR-VAL-002', policy_id='CARD-STD-001', description='Ambiguous')]
        self.reject('one declaration')
        self.issues[0]['evidence'] = [dict(type='METADATA', path='metadata/catalog/trans.yaml', description='Wrong scope')]
        self.reject('Catalog evidence scope')

    def test_resolved_effective_contract(self):
        self.decisions[0]['status'] = 'ACTIVE'
        self.reject('RESOLVED issue requires')

    def test_enums_and_dates_fail_closed(self):
        for container, key in [(self.issues[0], 'status'), (self.decisions[0], 'status'), (self.decisions[0], 'decision_type'), (self.issues[0]['evidence'][0], 'type')]:
            old = container[key]
            container[key] = 'unsupported'
            self.reject('Unsupported')
            container[key] = old
        self.decisions[0]['decision_date'] = '2026-99-01'
        self.reject('ValueError')

    def test_withheld_and_unknown_actor_date_remain_null(self):
        self.assertEqual(run_governance(self.config)['status'], 'SUCCESS')
        with duckdb.connect(str(self.config.database_path), read_only=True) as con:
            self.assertEqual(con.execute('SELECT actor,decision_date FROM governance_decisions').fetchall(), [(None,None)]*4)
            self.assertEqual(con.execute("SELECT count(*) FROM governance_decisions WHERE decision_type='REMEDIATION_WITHHELD' AND policy_id IS NULL").fetchone()[0], 3)

    def test_reference_ids_and_logical_rebuild_are_deterministic(self):
        before = load_registry(self.config)
        self.issues.reverse()
        self.decisions.reverse()
        self.issues[-1]['evidence'].reverse()
        self.save()
        self.assertEqual(before, load_registry(self.config))
        snapshots = []
        for _ in range(2):
            self.assertEqual(run_governance(self.config)['status'], 'SUCCESS')
            with duckdb.connect(str(self.config.database_path), read_only=True) as con:
                snapshots.append([con.execute(f'SELECT * FROM {t} ORDER BY 1').fetchall() for t in ['governance_issues','governance_decisions','governance_evidence']])
        self.assertEqual(*snapshots)

    def test_publication_failure_preserves_previous_database(self):
        self.assertEqual(run_governance(self.config)['status'], 'SUCCESS')
        before = self.config.database_path.read_bytes()
        with patch('src.governance.core.os.replace', side_effect=OSError('blocked replace')):
            self.assertEqual(run_governance(self.config)['status'], 'FAILED')
        self.assertEqual(before, self.config.database_path.read_bytes())
        self.assertFalse(list(self.config.database_path.parent.glob('*.staging*')))

    def test_validation_failure_preserves_previous_database(self):
        self.assertEqual(run_governance(self.config)['status'], 'SUCCESS')
        before = self.config.database_path.read_bytes()
        self.decisions[0]['policy_id'] = None
        self.reject('requires policy_id')
        self.assertEqual(before, self.config.database_path.read_bytes())

    def test_post_publication_cleanup_is_best_effort(self):
        real_replace = __import__('os').replace
        def replace_and_leave_cleanup(source, destination):
            real_replace(source, destination)
            Path(source).write_text('cleanup remainder')
        original_unlink = Path.unlink
        def fail_staging(path, *args, **kwargs):
            if path.name.endswith('.staging'):
                raise OSError('cleanup failure')
            return original_unlink(path, *args, **kwargs)
        with patch('src.governance.core.os.replace', side_effect=replace_and_leave_cleanup), patch.object(Path, 'unlink', fail_staging):
            self.assertEqual(run_governance(self.config)['status'], 'SUCCESS')
        with duckdb.connect(str(self.config.database_path), read_only=True) as con:
            self.assertEqual(con.execute('SELECT count(*) FROM governance_issues').fetchone()[0], 4)

    def test_acceptance_queries_and_no_execution_edges(self):
        self.assertEqual(run_governance(self.config)['status'], 'SUCCESS')
        doc = (Path(__file__).resolve().parents[1] / 'docs/architecture/governance_decisions.md').read_text(encoding='utf-8')
        queries = re.findall(r'```sql\n(.*?)\n```', doc, re.S)
        self.assertEqual(len(queries), 8)
        with duckdb.connect(str(self.config.database_path), read_only=True) as con:
            results = [con.execute(q).fetchall() for q in queries]
            self.assertEqual(list(map(len,results)), [4,3,4,3,3,3,4,3])
            self.assertEqual({r[0] for r in results[4]}, {'GOV-ISS-002','GOV-ISS-003','GOV-ISS-004'})
            self.assertTrue(all(r[1:] == (None,None) for r in results[6]))
            tables = {r[0] for r in con.execute('SHOW TABLES').fetchall()}
            self.assertEqual(tables, {'governance_issues','governance_decisions','governance_evidence'})
            columns = [r[1] for t in tables for r in con.execute(f"PRAGMA table_info('{t}')").fetchall()]
            self.assertFalse({'upstream_run_id','downstream_run_id','source_run_id'} & set(columns))
