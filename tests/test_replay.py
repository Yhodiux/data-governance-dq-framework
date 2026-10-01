from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import yaml

from src.replay import ReplayConfig, run_replay
from src.replay.core import parse_cutoff, parse_temporal
from src.standardization.core import sha256


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "trusted"
        self.catalog = self.root / "catalog"
        self.source.mkdir()
        self.catalog.mkdir()
        self.rows = {
            "parent": b'"id";"date"\r\n1;931231\r\n2;940101\r\n',
            "child": b'id;parent;date;label\n10;1;931231;" VYBER "\n11;1;940101;" "\n12;2;931201;"keep"',
            "link": b'id;parent\r\n20;1\r\n21;2\r\n',
            "person": b'id;label\n20;" "\n21;"VYBER"\n',
            "static": b'id;label\r\n1;"x;y"\r\n2;"a""b"',
        }
        self.columns = {"parent": ["id", "date"], "child": ["id", "parent", "date", "label"],
                        "link": ["id", "parent"], "person": ["id", "label"], "static": ["id", "label"]}
        for name, data in self.rows.items():
            (self.source / (name + ".asc")).write_bytes(data)
            (self.catalog / (name + ".yaml")).write_text(yaml.safe_dump({
                "asset": {"name": name, "source_file": name + ".asc"},
                "columns": [{"name": c, **({"documented_format": "YYMMDD"} if c == "date" else {})} for c in self.columns[name]],
            }), encoding="utf-8")
        temporal = {"column": "date", "format": "YYMMDD", "century": 1900}
        self.policy = {"assets": {
            "person": {"strategy": "reference", "references": [self.ref("id", "link", "id", "person_link")]},
            "child": {"strategy": "temporal_and_reference", "temporal": temporal, "references": [self.ref("parent", "parent", "id", "child_parent")]},
            "link": {"strategy": "reference", "references": [self.ref("parent", "parent", "id", "link_parent")]},
            "parent": {"strategy": "temporal", "temporal": temporal},
            "static": {"strategy": "static"},
        }}
        self.relationships = {"relationships": [
            {"name": "person_link", "from": {"asset": "link", "column": "id"}, "to": {"asset": "person", "column": "id"}},
            {"name": "child_parent", "from": {"asset": "child", "column": "parent"}, "to": {"asset": "parent", "column": "id"}},
            {"name": "link_parent", "from": {"asset": "link", "column": "parent"}, "to": {"asset": "parent", "column": "id"}},
        ]}
        self.manifest = self.root / "manifest.yaml"
        self.manifest.write_text(yaml.safe_dump({"dataset": {
            "physical_format": {"delimiter": ";", "text_qualifier": '"', "header": True},
            "files": [{"name": n + ".asc", "expected_record_count": 0, "sha256": "0" * 64} for n in self.rows],
        }}), encoding="utf-8")

    @staticmethod
    def ref(column, asset, target, relationship):
        return dict(column=column, asset=asset, target_column=target, relationship=relationship)

    def run_build(self, cutoff="1993-12-31"):
        policy = self.root / "policy.yaml"
        relationships = self.root / "relationships.yaml"
        policy.write_text(yaml.safe_dump(self.policy, sort_keys=False), encoding="utf-8")
        relationships.write_text(yaml.safe_dump(self.relationships), encoding="utf-8")
        self.config = ReplayConfig(self.manifest, self.catalog, relationships, policy, self.source,
                                   self.root / "snapshots", self.root / "results", cutoff)
        return run_replay(self.config)

    def hashes(self, path):
        return {p.name: sha256(p) for p in path.glob("*.asc")}

    def test_strategies_inclusive_cutoff_physical_preservation_and_no_future(self):
        result = self.run_build()
        self.assertEqual(result["status"], "SUCCESS", result["errors"])
        snapshot = Path(result["snapshot_path"])
        self.assertEqual((snapshot / "parent.asc").read_bytes(), b'"id";"date"\r\n1;931231\r\n')
        self.assertEqual((snapshot / "child.asc").read_bytes(), b'id;parent;date;label\n10;1;931231;" VYBER "\n')
        self.assertEqual((snapshot / "link.asc").read_bytes(), b'id;parent\r\n20;1\r\n')
        self.assertEqual((snapshot / "person.asc").read_bytes(), b'id;label\n20;" "\n')
        self.assertEqual((snapshot / "static.asc").read_bytes(), self.rows["static"])
        self.assertEqual(len(list(snapshot.glob("*.asc"))), 5)

    def test_before_and_after_cutoffs(self):
        before = self.run_build("1992-12-31")
        self.assertEqual(before["status"], "SUCCESS")
        self.assertTrue(all(a["output_rows"] == 0 for a in before["assets"] if a["asset"] != "static"))
        after = self.run_build("2000-01-01")
        self.assertEqual(after["status"], "SUCCESS")
        self.assertEqual(self.hashes(Path(after["snapshot_path"])), self.hashes(self.source))

    def test_determinism_rebuild_immutability_and_yaml_order(self):
        original = self.hashes(self.source)
        first = self.run_build()
        hashes = self.hashes(Path(first["snapshot_path"]))
        self.policy["assets"] = dict(reversed(list(self.policy["assets"].items())))
        second = self.run_build()
        self.assertEqual(second["status"], "SUCCESS")
        self.assertEqual(hashes, self.hashes(Path(second["snapshot_path"])))
        self.assertEqual(original, self.hashes(self.source))

    def test_bad_dates_abort_even_outside_selected_references(self):
        for value in ("", "940230", "940101 00:00:00", "9401", "abcdef", " 940101"):
            with self.subTest(value=value):
                (self.source / "child.asc").write_bytes(self.rows["child"] + ("\n13;2;" + value + ";x\n").encode())
                result = self.run_build()
                self.assertEqual(result["status"], "FAILED")
                self.assertTrue(result["errors"])
                self.assertFalse(Path(result["snapshot_path"]).exists())

    def test_explicit_century_and_iso_cutoff(self):
        self.assertEqual(parse_temporal("680101", {"century": 1900}).year, 1968)
        for value in ("1993-2-01", "1993-02-30", "../1993-01-01"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_cutoff(value)
        self.assertEqual(self.run_build("invalid")["status"], "FAILED")

    def test_metadata_rejections(self):
        original = deepcopy(self.policy)
        mutations = [
            lambda p: p["assets"].update(unknown={"strategy": "static"}),
            lambda p: p["assets"]["parent"].update(strategy="bad"),
            lambda p: p["assets"]["parent"]["temporal"].update(column="missing"),
            lambda p: p["assets"]["parent"]["temporal"].pop("century"),
            lambda p: p["assets"]["parent"]["temporal"].update(century=2000),
            lambda p: p["assets"]["link"]["references"][0].update(asset="missing"),
            lambda p: p["assets"]["link"]["references"][0].update(target_column="missing"),
            lambda p: p["assets"]["link"]["references"][0].update(relationship="bad"),
            lambda p: p["assets"]["link"].update(references=[]),
        ]
        for mutation in mutations:
            self.policy = deepcopy(original)
            mutation(self.policy)
            result = self.run_build()
            self.assertEqual(result["status"], "FAILED", self.policy)
            self.assertFalse(Path(result["snapshot_path"]).exists())

    def test_cycle(self):
        self.policy["assets"]["parent"] = {"strategy": "reference", "references": [self.ref("id", "link", "parent", "link_parent")]}
        result = self.run_build()
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("cycle", result["errors"][0])

    def test_orphan_in_full_source_fails(self):
        (self.source / "child.asc").write_bytes(self.rows["child"] + b'\n13;999;990101;x\n')
        result = self.run_build()
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("Orphan", result["errors"][0])

    def test_reverse_reference_orphan_fails(self):
        (self.source / "person.asc").write_bytes(b'id;label\n20;x\n')
        self.assertEqual(self.run_build()["status"], "FAILED")

    def test_failed_rebuild_preserves_previous_snapshot(self):
        first = self.run_build()
        snapshot = Path(first["snapshot_path"])
        original = self.hashes(snapshot)
        (self.source / "parent.asc").write_bytes(b'id;date\n1;bad\n')
        self.assertEqual(self.run_build()["status"], "FAILED")
        self.assertEqual(original, self.hashes(snapshot))

    def test_staging_validation_failure_cleans_staging(self):
        first = self.run_build()
        original = self.hashes(Path(first["snapshot_path"]))
        from src.replay.core import scan
        calls = 0

        def failing_scan(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("staged failure")
            return scan(*args, **kwargs)

        with patch("src.replay.core.scan", side_effect=failing_scan):
            result = self.run_build()
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(original, self.hashes(Path(first["snapshot_path"])))
        self.assertFalse(list((self.root / "snapshots").glob(".*-staging-*")))

    def test_publish_failure_rolls_back_previous_snapshot(self):
        first = self.run_build()
        original = self.hashes(Path(first["snapshot_path"]))
        replace = Path.replace

        def fail_staging(path, target):
            if "-staging-" in path.name:
                raise OSError("publication failure")
            return replace(path, target)

        with patch.object(Path, "replace", fail_staging):
            result = self.run_build()
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(original, self.hashes(Path(first["snapshot_path"])))
        self.assertFalse(list((self.root / "snapshots").glob(".*")))

    def test_record_contains_contract(self):
        result = self.run_build()
        record = json.loads(Path(result["execution_record"]).read_text(encoding="utf-8"))
        for field in ("run_id", "started_at", "completed_at", "elapsed_seconds", "cutoff", "source_zone", "source_path", "snapshot_path", "metadata", "assets", "errors"):
            self.assertIn(field, record)
        self.assertEqual(record["source_zone"], "trusted")

    def test_multiline_or_malformed_record_fails(self):
        (self.source / "static.asc").write_bytes(b'id;label\n1;"line\nbreak"\n')
        self.assertEqual(self.run_build()["status"], "FAILED")

    def test_missing_asset_fails(self):
        (self.source / "static.asc").unlink()
        self.assertEqual(self.run_build()["status"], "FAILED")

    def test_execution_record_failure_rolls_back(self):
        first = self.run_build()
        original = self.hashes(Path(first["snapshot_path"]))
        with patch("src.replay.core.write_execution_record", side_effect=OSError("disk failure")):
            result = self.run_build()
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(original, self.hashes(Path(first["snapshot_path"])))
        self.assertFalse(list((self.root / "snapshots").glob(".*")))

    def test_execution_record_failure_removes_first_publication(self):
        with patch("src.replay.core.write_execution_record", side_effect=OSError("disk failure")):
            result = self.run_build()
        self.assertEqual(result["status"], "FAILED")
        self.assertFalse(Path(result["snapshot_path"]).exists())

    def test_staging_creation_failure_preserves_existing_snapshot(self):
        first = self.run_build()
        original = self.hashes(Path(first["snapshot_path"]))
        mkdir = Path.mkdir

        def fail_staging(path, *args, **kwargs):
            if "-staging-" in path.name:
                raise OSError("cannot create staging")
            return mkdir(path, *args, **kwargs)

        with patch.object(Path, "mkdir", fail_staging):
            result = self.run_build()
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(original, self.hashes(Path(first["snapshot_path"])))
