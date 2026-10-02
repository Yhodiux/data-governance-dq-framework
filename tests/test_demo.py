"""Orchestration tests never execute a pipeline subprocess."""

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from src.demo.__main__ import CUTOFFS, EVIDENCE, REQUIRED, run_demo


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'external source'
        self.source.mkdir()
        for relative in (*REQUIRED, *EVIDENCE, 'metadata/catalog/card.yaml'):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}', encoding='utf-8')
        (self.root / 'config/dataset_manifest.yaml').write_text(
            yaml.safe_dump({'dataset': {'files': [{'name': 'card.asc'}]}}), encoding='utf-8')
        (self.source / 'card.asc').write_text('synthetic fixture', encoding='utf-8')
        self.output = StringIO()

    def run_with(self, **kwargs):
        with redirect_stdout(self.output), patch('src.demo.__main__.subprocess.run', **kwargs) as runner:
            code = run_demo(self.source, self.root)
        return code, runner

    def test_complete_order_six_pairs_and_success(self):
        code, runner = self.run_with(return_value=SimpleNamespace(returncode=0))
        self.assertEqual(code, 0)
        calls = runner.call_args_list
        self.assertEqual(len(calls), 24)
        modules = [c.args[0][2] for c in calls]
        self.assertEqual(modules[:6], ['src.ingestion','src.profiling','src.catalog','src.dq','src.standardization','src.dq'])
        self.assertEqual(modules[18:], ['src.lineage','src.traceability','src.observability','src.metrics','src.governance','src.reporting'])
        for index, cutoff in enumerate(CUTOFFS):
            replay = calls[6+index*2].args[0]
            dq = calls[7+index*2].args[0]
            self.assertEqual(replay[2:], ['src.replay','--cutoff',cutoff])
            self.assertEqual(dq[2:], ['src.dq','--data-path',f'data/snapshots/{cutoff}',
                                     '--data-zone','snapshot','--snapshot-cutoff',cutoff])
        self.assertEqual(calls[0].args[0][-1],str(self.source.resolve()))
        for call in calls:
            self.assertEqual(call.args[0][:2], [sys.executable,'-m'])
            self.assertEqual(call.kwargs, {'cwd': self.root.resolve(), 'check': False})
        self.assertTrue(self.output.getvalue().rstrip().endswith('End-to-end demo: SUCCESS'))

    def test_preflight_missing_source_metadata_and_each_evidence(self):
        paths = [self.source/'card.asc', self.root/'metadata/catalog/card.yaml',
                 self.root/'metadata/relationships.yaml', *(self.root/p for p in EVIDENCE)]
        for path in paths:
            with self.subTest(path=path):
                content = path.read_bytes()
                path.unlink()
                code, runner = self.run_with(return_value=SimpleNamespace(returncode=0))
                self.assertEqual(code,1)
                runner.assert_not_called()
                path.write_bytes(content)
        self.source = self.root/'missing directory'
        code, runner = self.run_with(return_value=SimpleNamespace(returncode=0))
        self.assertEqual(code,1)
        runner.assert_not_called()

    def test_fail_fast_dq_engine_and_final_reporting(self):
        for stage in [0,3,8,23]:
            with self.subTest(stage=stage):
                self.output = StringIO()
                results = [SimpleNamespace(returncode=0)]*stage + [SimpleNamespace(returncode=7)]
                code, runner = self.run_with(side_effect=results)
                self.assertEqual(code,7)
                self.assertEqual(runner.call_count,stage+1)
                self.assertNotIn('End-to-end demo: SUCCESS',self.output.getvalue())

    def test_rule_findings_do_not_override_successful_cli_status(self):
        def successful_cli(*args, **kwargs):
            print('DQ execution SUCCESS: 16 passed, 4 failed')
            return SimpleNamespace(returncode=0)
        code, runner = self.run_with(side_effect=successful_cli)
        self.assertEqual(code,0)
        self.assertEqual(runner.call_count,24)
        self.assertIn('End-to-end demo: SUCCESS',self.output.getvalue())

    def test_subprocess_launch_error_stops_demo(self):
        code, runner = self.run_with(side_effect=OSError('interpreter unavailable'))
        self.assertEqual(code,1)
        self.assertEqual(runner.call_count,1)
        self.assertNotIn('End-to-end demo: SUCCESS',self.output.getvalue())
