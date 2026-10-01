"""Synthetic structural-contract tests; no banking data or history required."""

import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import duckdb
import yaml

from src.lineage.core import (
    CONTRACTS, EDGE_CATEGORIES, Evidence, LineageConfig, build_graph,
    edge_identifier, identifier, insert_unique, load_metadata, run_lineage,
    validate_graph,
)


class LineageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = LineageConfig(self.root)
        project = Path(__file__).resolve().parents[1]
        for path in {p for pairs in CONTRACTS.values() for p, _ in pairs}:
            destination = self.root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(project / path, destination)
        self.write("config/dataset_manifest.yaml", {"dataset": {
            "identifier": "synthetic", "files": [{"name": a + ".asc"} for a in ("account", "card", "trans")]
        }})
        columns = {
            "account": [{"name": "account_id"}],
            "card": [{"name": "issued", "documented_format": "YYMMDD"}, {"name": "type"}],
            "trans": [{"name": "account_id"}, {"name": "type", "documented_values": {"PRIJEM": "credit", "VYDAJ": "debit"}}],
        }
        for asset, fields in columns.items():
            self.write(f"metadata/catalog/{asset}.yaml", {
                "asset": {"name": asset, "source_file": asset + ".asc"}, "columns": fields,
            })
        self.write("metadata/relationships.yaml", {"relationships": [{
            "name": "transaction_to_account", "from": {"asset": "trans", "column": "account_id"},
            "to": {"asset": "account", "column": "account_id"},
            "evidence": {"type": "source_documentation", "document": "synthetic"},
        }]})
        self.write("metadata/dq_rules/rules.yaml", {"rules": [
            {"id": "CAR-VAL-002", "asset": "card", "column": "issued", "dimension": "validity",
             "expectation": {"type": "regex_format", "pattern": r"^\d{6}$"}, "empty_policy": "evaluate",
             "evidence": {"type": "catalog", "reference": "card.issued"}},
            {"id": "TRA-VAL-002", "asset": "trans", "column": "type", "dimension": "validity",
             "expectation": {"type": "allowed_values", "values": ["PRIJEM", "VYDAJ"]}, "empty_policy": "evaluate",
             "evidence": {"type": "catalog", "reference": "trans.type"}},
        ]})
        self.write("metadata/standardization/card.yaml", {"policies": [{
            "id": "CARD-STD-001", "asset": "card", "column": "issued",
            "transformation": {"type": "regex_replace", "pattern": r"^(\d{6}) 00:00:00$", "replacement": r"\1"},
            "evidence": {"type": "catalog", "reference": "card.issued", "description": "Documented format"},
        }]})
        self.write("metadata/replay/historical_snapshot.yaml", {"assets": {
            "account": {"strategy": "static"}, "card": {"strategy": "static"},
            "trans": {"strategy": "reference", "references": [{"column": "account_id", "asset": "account",
                "target_column": "account_id", "relationship": "transaction_to_account"}]},
        }})

    def write(self, relative, document):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    def mutate(self, relative, operation):
        document = yaml.safe_load((self.root / relative).read_text(encoding="utf-8"))
        operation(document)
        self.write(relative, document)

    def evidence(self):
        evidence = Evidence(self.root)
        load_metadata(self.root, evidence)
        for component in CONTRACTS:
            evidence.contracts(component)
        return evidence

    def rows(self):
        with duckdb.connect(str(self.config.database_path), read_only=True) as db:
            return (
                db.execute("SELECT * FROM lineage_nodes ORDER BY node_id").fetchall(),
                db.execute("SELECT * FROM lineage_edges ORDER BY edge_id").fetchall(),
            )

    def test_logical_determinism_and_input_order(self):
        first = build_graph(self.config)
        self.mutate("metadata/dq_rules/rules.yaml", lambda d: d["rules"].reverse())
        self.mutate("config/dataset_manifest.yaml", lambda d: d["dataset"]["files"].reverse())
        self.assertEqual(first, build_graph(self.config))

    def test_unique_ids_and_existing_endpoints(self):
        nodes, edges = build_graph(self.config)
        self.assertEqual(len(nodes), len({n["node_id"] for n in nodes.values()}))
        self.assertEqual(len(edges), len({e["edge_id"] for e in edges.values()}))
        for edge in edges.values():
            self.assertIn(edge["source_node_id"], nodes)
            self.assertIn(edge["target_node_id"], nodes)

    def test_id_encoding_and_conflicting_duplicates(self):
        self.assertNotEqual(identifier("column", "raw", "a:b", "c"), identifier("column", "raw", "a", "b:c"))
        self.assertNotEqual(identifier("rule", "a:b"), identifier("rule", "a%3Ab"))
        for label in ("node", "edge"):
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, "Conflicting duplicate"):
                insert_unique({label: {"value": 1}}, label, {"value": 2})

    def test_exact_categories_and_types(self):
        _, edges = build_graph(self.config)
        self.assertEqual({e["edge_type"] for e in edges.values()}, set(EDGE_CATEGORIES))
        for edge in edges.values():
            self.assertEqual(edge["edge_category"], EDGE_CATEGORIES[edge["edge_type"]])

    def test_source_raw_asset_flows_only(self):
        nodes, edges = build_graph(self.config)
        flows = [e for e in edges.values() if e["edge_type"] == "ingested_to"]
        self.assertEqual(len(flows), 3)
        for edge in flows:
            self.assertEqual(nodes[edge["source_node_id"]]["node_type"], "asset")
            self.assertEqual(nodes[edge["source_node_id"]]["zone"], "source")
            self.assertEqual(nodes[edge["target_node_id"]]["zone"], "raw")
        self.assertFalse(any(n["node_type"] == "column" and n["zone"] == "source" for n in nodes.values()))

    def test_all_assets_published_including_untransformed(self):
        nodes, edges = build_graph(self.config)
        flows = [e for e in edges.values() if e["edge_type"] == "published_to"]
        self.assertEqual({nodes[e["source_node_id"]]["asset"] for e in flows}, {"account", "card", "trans"})
        for edge in flows:
            self.assertEqual((nodes[edge["source_node_id"]]["zone"], nodes[edge["target_node_id"]]["zone"]), ("raw", "trusted"))

    def test_only_policy_column_transformed_with_context(self):
        nodes, edges = build_graph(self.config)
        transforms = [e for e in edges.values() if e["edge_type"] == "transformed_by"]
        self.assertEqual(len(transforms), 1)
        edge = transforms[0]
        self.assertEqual(edge["source_node_id"], "column:raw:card:issued")
        self.assertEqual(edge["target_node_id"], "column:trusted:card:issued")
        self.assertEqual(edge["context_node_id"], "policy:CARD-STD-001")
        self.assertEqual(nodes[edge["context_node_id"]]["node_type"], "standardization_policy")
        self.assertFalse(any(e["source_node_id"].startswith("policy:") or e["target_node_id"].startswith("policy:") for e in edges.values()))

    def test_one_rule_node_shared_by_three_zones_and_no_transformation(self):
        nodes, edges = build_graph(self.config)
        self.assertEqual(sum(n["node_type"] == "dq_rule" for n in nodes.values()), 2)
        for rule in ("rule:CAR-VAL-002", "rule:TRA-VAL-002"):
            associations = [e for e in edges.values() if e["target_node_id"] == rule]
            self.assertEqual(len(associations), 3)
            self.assertEqual({nodes[e["source_node_id"]]["zone"] for e in associations}, {"raw", "trusted", "snapshot"})
            self.assertTrue(all(e["edge_category"] == "EVALUATION" for e in associations))
        self.assertFalse(any(e["source_node_id"].startswith("rule:") for e in edges.values()))

    def test_relationship_once_on_logical_columns(self):
        nodes, edges = build_graph(self.config)
        references = [e for e in edges.values() if e["edge_type"] == "references"]
        self.assertEqual(len(references), 1)
        self.assertEqual(references[0]["source_node_id"], "column:logical:trans:account_id")
        self.assertEqual(references[0]["target_node_id"], "column:logical:account:account_id")
        self.assertTrue(all(nodes[references[0][side]]["zone"] == "logical" for side in ("source_node_id", "target_node_id")))

    def test_snapshot_structural_and_no_dependency_flow(self):
        nodes, edges = build_graph(self.config)
        replayed = [e for e in edges.values() if e["edge_type"] == "replayed_to"]
        self.assertEqual(len(replayed), 3)
        for edge in edges.values():
            if edge["edge_category"] == "DATA_FLOW":
                self.assertEqual(nodes[edge["source_node_id"]]["asset"], nodes[edge["target_node_id"]]["asset"])
        serialized = json.dumps([nodes, edges])
        for forbidden in ("cutoff", "run_id", "data/snapshots/", "compliance_ratio", "violations"):
            self.assertNotIn(forbidden, serialized)

    def test_evidence_present_resolvable_and_sorted(self):
        _, edges = build_graph(self.config)
        evidence = self.evidence()
        for edge in edges.values():
            self.assertTrue(edge["evidence"])
            serialized = [json.dumps(r, sort_keys=True, separators=(",", ":")) for r in edge["evidence"]]
            self.assertEqual(serialized, sorted(set(serialized)))
            for reference in edge["evidence"]:
                evidence.validate(reference)
                self.assertFalse(Path(reference["path"]).is_absolute())
                self.assertNotIn("line", reference["selector"])

    def test_builder_without_data_or_execution_history(self):
        self.assertFalse((self.root / "data").exists())
        original = Path.open
        def guarded(path, *args, **kwargs):
            relative = path.resolve().relative_to(self.root).as_posix()
            denied = ("data/raw", "data/trusted", "data/snapshots", "data/results/dq", "data/results/replay", "data/results/observability")
            if any(relative == prefix or relative.startswith(prefix + "/") for prefix in denied):
                raise AssertionError("Forbidden lineage read: " + relative)
            return original(path, *args, **kwargs)
        with patch.object(Path, "open", guarded):
            result = run_lineage(self.config)
        self.assertEqual(result["status"], "SUCCESS", result["errors"])
        record = json.loads(Path(result["execution_record"]).read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "SUCCESS")
        self.assertNotIn("source_runs", record)

    def test_invalid_metadata_scopes_and_coverage(self):
        cases = [
            ("config/dataset_manifest.yaml", lambda d: d["dataset"]["files"].pop()),
            ("metadata/dq_rules/rules.yaml", lambda d: d["rules"][0].update(asset="missing")),
            ("metadata/dq_rules/rules.yaml", lambda d: d["rules"][0].update(column="missing")),
            ("metadata/standardization/card.yaml", lambda d: d["policies"][0].update(asset="missing")),
            ("metadata/standardization/card.yaml", lambda d: d["policies"][0].update(column="missing")),
            ("metadata/relationships.yaml", lambda d: d["relationships"][0]["to"].update(column="missing")),
            ("metadata/replay/historical_snapshot.yaml", lambda d: d["assets"].update(missing={"strategy": "static"})),
            ("metadata/catalog/card.yaml", lambda d: d["columns"].append(d["columns"][0])),
            ("metadata/dq_rules/rules.yaml", lambda d: d["rules"].append(d["rules"][0])),
            ("metadata/standardization/card.yaml", lambda d: d["policies"].append(d["policies"][0])),
        ]
        for path, operation in cases:
            with self.subTest(path=path, operation=operation):
                original = (self.root / path).read_bytes()
                try:
                    self.mutate(path, operation)
                    result = run_lineage(self.config)
                    self.assertEqual(result["status"], "FAILED")
                    self.assertTrue(result["errors"])
                    self.assertFalse(self.config.database_path.exists())
                finally:
                    (self.root / path).write_bytes(original)

    def test_duplicate_yaml_keys_rejected(self):
        path = self.root / "metadata/replay/historical_snapshot.yaml"
        path.write_text("assets: {}\nassets: {}\n", encoding="utf-8")
        result = run_lineage(self.config)
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("Duplicate YAML key", result["errors"][0])

    def test_invalid_graph_edges_and_evidence(self):
        nodes, original = build_graph(self.config)
        evidence = self.evidence()
        for fault in ("endpoint", "category", "context", "scope", "empty", "unresolved"):
            with self.subTest(fault=fault):
                edges = copy.deepcopy(original)
                edge = next(e for e in edges.values() if e["edge_type"] == "transformed_by")
                old_key = edge["edge_id"]
                if fault == "endpoint": edge["target_node_id"] = "missing"
                if fault == "category": edge["edge_category"] = "EVALUATION"
                if fault == "context": edge["context_node_id"] = "rule:CAR-VAL-002"
                if fault == "scope": edge["target_node_id"] = "column:trusted:trans:type"
                if fault == "empty": edge["evidence"] = []
                if fault == "unresolved": edge["evidence"][0]["selector"] = {"id": "missing"}
                del edges[old_key]
                edge["edge_id"] = edge_identifier(edge["source_node_id"], edge["target_node_id"], edge["edge_type"], edge["context_node_id"])
                edges[edge["edge_id"]] = edge
                with self.assertRaises(ValueError):
                    validate_graph(nodes, edges, evidence)

    def test_failed_build_preserves_previous_database(self):
        self.assertEqual(run_lineage(self.config)["status"], "SUCCESS")
        before = self.config.database_path.read_bytes()
        self.mutate("metadata/dq_rules/rules.yaml", lambda d: d["rules"][0].update(column="missing"))
        self.assertEqual(run_lineage(self.config)["status"], "FAILED")
        self.assertEqual(before, self.config.database_path.read_bytes())
        self.assertFalse(list(self.config.database_path.parent.glob("*.staging-*")))

    def test_failed_staging_constraint_and_replace_preserve_database(self):
        self.assertEqual(run_lineage(self.config)["status"], "SUCCESS")
        before = self.config.database_path.read_bytes()
        nodes, edges = build_graph(self.config)
        bad_edges = copy.deepcopy(edges)
        next(iter(bad_edges.values()))["target_node_id"] = "missing"
        with patch("src.lineage.core.build_graph", return_value=(nodes, bad_edges)):
            self.assertEqual(run_lineage(self.config)["status"], "FAILED")
        self.assertEqual(before, self.config.database_path.read_bytes())
        with patch("src.lineage.core.os.replace", side_effect=OSError("publication denied")):
            self.assertEqual(run_lineage(self.config)["status"], "FAILED")
        self.assertEqual(before, self.config.database_path.read_bytes())
        self.assertFalse(list(self.config.database_path.parent.glob("*.staging-*")))

    def test_post_publication_cleanup_failure_keeps_success(self):
        original_replace, original_exists, original_unlink = os.replace, Path.exists, Path.unlink
        for failure in ("exists", "unlink"):
            with self.subTest(failure=failure):
                replaced = []
                def replace(source, target):
                    original_replace(source, target)
                    if Path(target) == self.config.database_path.resolve():
                        replaced.append(Path(source))
                        if failure == "unlink":
                            Path(str(source) + ".wal").write_text("residue", encoding="utf-8")
                def exists(path):
                    if failure == "exists" and path in replaced:
                        raise OSError("post-publication cleanup check failed")
                    return original_exists(path)
                def unlink(path, *args, **kwargs):
                    if failure == "unlink" and any(path == Path(str(p) + ".wal") for p in replaced):
                        raise OSError("post-publication cleanup deletion failed")
                    return original_unlink(path, *args, **kwargs)
                # Remove the previous output so successful publication is observable.
                if self.config.database_path.exists():
                    self.config.database_path.unlink()
                with patch("src.lineage.core.os.replace", side_effect=replace), \
                     patch.object(Path, "exists", exists), patch.object(Path, "unlink", unlink):
                    result = run_lineage(self.config)
                self.assertTrue(replaced)
                self.assertEqual(result["status"], "SUCCESS", result["errors"])
                self.assertEqual(result["errors"], [])
                self.assertTrue(self.rows()[0])
                record = json.loads(Path(result["execution_record"]).read_text(encoding="utf-8"))
                self.assertEqual(record["status"], "SUCCESS")

    def test_successful_database_record_write_failure_is_auxiliary(self):
        def partial_dump(record, output, **kwargs):
            output.write('{"status":')
            raise OSError("record write failed")
        with patch("src.lineage.core.json.dump", side_effect=partial_dump):
            result = run_lineage(self.config)
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["execution_record"], "UNAVAILABLE")
        self.assertIn("record write failed", result["errors"][0])
        self.assertTrue(self.rows()[0])
        self.assertEqual(list(self.config.runs_path.iterdir()), [])

    def test_successful_database_record_replace_failure_is_auxiliary(self):
        original_replace = os.replace
        def replace(source, target):
            if Path(target).suffix == ".json":
                raise OSError("record replace failed")
            return original_replace(source, target)
        with patch("src.lineage.core.os.replace", side_effect=replace):
            result = run_lineage(self.config)
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["execution_record"], "UNAVAILABLE")
        self.assertIn("record replace failed", result["errors"][0])
        self.assertTrue(self.rows()[0])
        self.assertEqual(list(self.config.runs_path.iterdir()), [])

    def test_failed_build_record_failure_leaves_no_partial_json(self):
        self.assertEqual(run_lineage(self.config)["status"], "SUCCESS")
        previous_database = self.config.database_path.read_bytes()
        previous_records = set(self.config.runs_path.iterdir())
        def partial_dump(record, output, **kwargs):
            output.write('{"status":')
            raise OSError("record write failed")
        for failure in ("write", "replace"):
            with self.subTest(failure=failure), \
                 patch("src.lineage.core.build_graph", side_effect=ValueError("build failed")):
                target = "src.lineage.core.json.dump" if failure == "write" else "src.lineage.core.os.replace"
                effect = partial_dump if failure == "write" else OSError("record replace failed")
                with patch(target, side_effect=effect):
                    result = run_lineage(self.config)
                self.assertEqual(result["status"], "FAILED")
                self.assertEqual(result["execution_record"], "UNAVAILABLE")
                self.assertIn("build failed", result["errors"][0])
                self.assertIn("Could not write build record", result["errors"][1])
                self.assertEqual(set(self.config.runs_path.iterdir()), previous_records)
                self.assertEqual(self.config.database_path.read_bytes(), previous_database)

    def test_normal_build_record_atomic_publication(self):
        original_replace = os.replace
        observed_records = []
        def replace(source, target):
            if Path(target).suffix == ".json":
                self.assertEqual(Path(source).parent, Path(target).parent)
                self.assertFalse(Path(target).exists())
                observed_records.append(json.loads(Path(source).read_text(encoding="utf-8")))
            return original_replace(source, target)
        with patch("src.lineage.core.os.replace", side_effect=replace):
            result = run_lineage(self.config)
        self.assertEqual(result["status"], "SUCCESS")
        expected = {key: value for key, value in result.items() if key != "execution_record"}
        record_path = Path(result["execution_record"])
        self.assertEqual(json.loads(record_path.read_text(encoding="utf-8")), expected)
        self.assertEqual(observed_records, [expected])
        self.assertEqual(list(self.config.runs_path.iterdir()), [record_path])

    def test_rebuild_replaces_and_does_not_duplicate(self):
        self.assertEqual(run_lineage(self.config)["status"], "SUCCESS")
        first = self.rows()
        self.assertEqual(run_lineage(self.config)["status"], "SUCCESS")
        self.assertEqual(first, self.rows())
        with duckdb.connect(str(self.config.database_path), read_only=True) as db:
            self.assertEqual(db.execute("SHOW TABLES").fetchall(), [("lineage_edges",), ("lineage_nodes",)])
            for table, key in (("lineage_nodes", "node_id"), ("lineage_edges", "edge_id")):
                count, distinct = db.execute(f"SELECT count(*), count(DISTINCT {key}) FROM {table}").fetchone()
                self.assertEqual(count, distinct)

    def test_contract_evidence_resolution_fails_closed(self):
        (self.root / "src/ingestion/core.py").write_text("def renamed(): pass\n", encoding="utf-8")
        result = run_lineage(self.config)
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("Unresolved contract", result["errors"][0])

    def test_documented_relationship_cycles_are_not_global_dag_errors(self):
        self.mutate("metadata/relationships.yaml", lambda d: d["relationships"].append({
            "name": "reverse", "from": {"asset": "account", "column": "account_id"},
            "to": {"asset": "trans", "column": "account_id"}, "evidence": {"document": "synthetic"},
        }))
        _, edges = build_graph(self.config)
        self.assertEqual(sum(e["edge_type"] == "references" for e in edges.values()), 2)

    def test_acceptance_queries_a_to_f(self):
        self.assertEqual(run_lineage(self.config)["status"], "SUCCESS")
        with duckdb.connect(str(self.config.database_path), read_only=True) as db:
            # A: related asset flows plus issued transformation and DQ, with policy context.
            a = db.execute("""SELECT e.edge_type, s.node_id, t.node_id, e.context_node_id
                FROM lineage_edges e JOIN lineage_nodes s ON s.node_id=e.source_node_id
                JOIN lineage_nodes t ON t.node_id=e.target_node_id
                WHERE s.asset='card' AND (s.node_type='asset' OR s.column_name='issued')
                ORDER BY e.edge_type,s.node_id""").fetchall()
            self.assertEqual(len(a), 7)
            self.assertIn(("transformed_by", "column:raw:card:issued", "column:trusted:card:issued", "policy:CARD-STD-001"), a)
            # B: distinct semantic rule IDs, shared across zones.
            self.assertEqual(db.execute("""SELECT DISTINCT t.semantic_id FROM lineage_edges e
                JOIN lineage_nodes s ON s.node_id=e.source_node_id JOIN lineage_nodes t ON t.node_id=e.target_node_id
                WHERE e.edge_type='evaluated_by' AND s.asset='trans' AND s.column_name='type'""").fetchall(), [("TRA-VAL-002",)])
            # C: only explicit policy columns.
            self.assertEqual(db.execute("""SELECT s.asset,s.column_name,p.semantic_id FROM lineage_edges e
                JOIN lineage_nodes s ON s.node_id=e.source_node_id JOIN lineage_nodes p ON p.node_id=e.context_node_id
                WHERE e.edge_type='transformed_by'""").fetchall(), [("card", "issued", "CARD-STD-001")])
            # D: asset-level snapshot applicability.
            self.assertEqual(db.execute("""SELECT s.asset FROM lineage_edges e JOIN lineage_nodes s ON s.node_id=e.source_node_id
                WHERE e.edge_type='replayed_to' ORDER BY s.asset""").fetchall(), [("account",), ("card",), ("trans",)])
            # E: documented references, logical only.
            self.assertEqual(db.execute("""SELECT s.node_id,t.node_id FROM lineage_edges e
                JOIN lineage_nodes s ON s.node_id=e.source_node_id JOIN lineage_nodes t ON t.node_id=e.target_node_id
                WHERE e.edge_type='references' AND s.asset='trans'""").fetchall(), [("column:logical:trans:account_id", "column:logical:account:account_id")])
            # F: every persisted edge has parseable, resolvable evidence.
            evidence = self.evidence()
            for _, value in db.execute("SELECT edge_id,evidence FROM lineage_edges ORDER BY edge_id").fetchall():
                refs = json.loads(value)
                self.assertTrue(refs)
                for ref in refs:
                    evidence.validate(ref)


if __name__ == "__main__":
    unittest.main()
