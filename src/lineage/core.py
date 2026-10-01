"""Project current structural contracts; never read data or execution history."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import duckdb
import yaml

from src.dq.core import validate_rule_metadata
from src.replay.core import resolve_metadata
from src.standardization.core import validate_policies

EDGE_CATEGORIES = {
    "ingested_to": "DATA_FLOW",
    "published_to": "DATA_FLOW",
    "replayed_to": "DATA_FLOW",
    "transformed_by": "TRANSFORMATION",
    "evaluated_by": "EVALUATION",
    "references": "DATA_RELATIONSHIP",
}
ZONES = ("source", "raw", "trusted", "snapshot")
CONTRACTS = {
    "ingestion": [("src/ingestion/core.py", "publish_atomically"), ("src/ingestion/__main__.py", "main")],
    "standardization": [("src/standardization/core.py", "build_and_publish"), ("src/standardization/__main__.py", "main")],
    "replay": [("src/replay/core.py", "run_replay"), ("src/replay/__main__.py", "main")],
    "dq": [("src/dq/core.py", "run_dq"), ("src/dq/core.py", "DATA_ZONES")],
}


@dataclass(frozen=True)
class LineageConfig:
    root_path: Path

    @property
    def database_path(self) -> Path:
        return self.root_path / "data/results/lineage/lineage.duckdb"

    @property
    def runs_path(self) -> Path:
        return self.root_path / "data/results/lineage/runs"


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject ambiguous YAML declarations, including duplicate mapping keys."""


def unique_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError(f"Duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def identifier(prefix: str, *parts: str) -> str:
    return ":".join([prefix] + [quote(part, safe="") for part in parts])


def edge_identifier(source: str, target: str, edge_type: str, context: str | None) -> str:
    value = canonical([source, target, edge_type, context]).encode("utf-8")
    return "edge:" + hashlib.sha256(value).hexdigest()


def insert_unique(items: dict, key: str, value: dict) -> None:
    if key in items and items[key] != value:
        raise ValueError(f"Conflicting duplicate ID: {key}")
    items[key] = value


class Evidence:
    """Resolvable selectors registered only from authoritative inputs."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.registered = set()

    def metadata(self, path: str, kind: str, **scope) -> dict:
        reference = {"evidence_type": "metadata", "path": path, "selector": {"kind": kind, **scope}}
        self.registered.add(canonical(reference))
        return reference

    def contracts(self, component: str) -> list[dict]:
        references = []
        for path, symbol in CONTRACTS[component]:
            tree = ast.parse((self.root / path).read_text(encoding="utf-8"))
            functions = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            assignments = {
                t.id: n.value for n in tree.body if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name)
            }
            if symbol not in functions and symbol not in assignments:
                raise ValueError(f"Unresolved contract: {path}:{symbol}")
            if symbol == "DATA_ZONES" and ast.literal_eval(assignments[symbol]) != {"raw", "trusted", "snapshot"}:
                raise ValueError("DQ zone contract differs from the structural adapter")
            reference = {"evidence_type": "framework_contract", "path": path, "selector": {"symbol": symbol}}
            self.registered.add(canonical(reference))
            references.append(reference)
        return references

    def validate(self, reference: dict) -> None:
        if canonical(reference) not in self.registered:
            raise ValueError(f"Unresolved evidence: {reference}")
        path = self.root / reference["path"]
        if self.root not in path.resolve().parents or not path.is_file():
            raise ValueError(f"Evidence file unavailable: {reference['path']}")


def read_yaml(root: Path, path: str):
    try:
        return yaml.load((root / path).read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except Exception as exc:
        raise ValueError(f"{path}: {exc}") from exc


def require_text(value, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be nonempty text")
    return value


def load_metadata(root: Path, evidence: Evidence) -> dict:
    manifest_path = "config/dataset_manifest.yaml"
    manifest = read_yaml(root, manifest_path)
    dataset = manifest["dataset"]
    require_text(dataset["identifier"], "dataset.identifier")
    entries = dataset["files"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("Manifest must contain files")
    files, file_refs = set(), {}
    for entry in entries:
        name = require_text(entry["name"], "manifest file")
        if Path(name).name != name or name in {".", ".."} or name in files:
            raise ValueError(f"Invalid or duplicate manifest file: {name}")
        files.add(name)
        file_refs[name] = evidence.metadata(manifest_path, "manifest_file", name=name)

    catalog, asset_refs, column_refs = {}, {}, {}
    for path in sorted((root / "metadata/catalog").glob("*.yaml")):
        relative = path.relative_to(root).as_posix()
        document = read_yaml(root, relative)
        name = require_text(document["asset"]["name"], relative + ":asset.name")
        if name in catalog:
            raise ValueError(f"Duplicate catalog asset: {name}")
        source_file = require_text(document["asset"]["source_file"], relative + ":source_file")
        columns = {}
        for column in document["columns"]:
            column_name = require_text(column["name"], relative + ":column")
            if column_name in columns:
                raise ValueError(f"Duplicate catalog column: {name}.{column_name}")
            columns[column_name] = column
            column_refs[name, column_name] = evidence.metadata(relative, "catalog_column", asset=name, column=column_name)
        if not columns:
            raise ValueError(f"Catalog asset has no columns: {name}")
        catalog[name] = {"source_file": source_file, "columns": columns}
        asset_refs[name] = evidence.metadata(relative, "catalog_asset", asset=name)
    if len(catalog) != len(files) or {a["source_file"] for a in catalog.values()} != files:
        raise ValueError("Manifest/catalog coverage is incompatible")

    relationship_path = "metadata/relationships.yaml"
    relationships = read_yaml(root, relationship_path)["relationships"]
    relationship_map, relationship_refs = {}, {}
    for relationship in relationships:
        name = require_text(relationship["name"], "relationship.name")
        if name in relationship_map:
            raise ValueError(f"Duplicate relationship: {name}")
        for endpoint in (relationship["from"], relationship["to"]):
            asset, column = endpoint["asset"], endpoint["column"]
            if asset not in catalog or column not in catalog[asset]["columns"]:
                raise ValueError(f"Unknown relationship endpoint: {asset}.{column}")
        if not relationship.get("evidence"):
            raise ValueError(f"Relationship lacks documentary evidence: {name}")
        relationship_map[name] = relationship
        relationship_refs[name] = evidence.metadata(relationship_path, "relationship", name=name)

    def declarations(directory: str, key: str, kind: str):
        items, references, seen = [], {}, set()
        for path in sorted((root / directory).glob("*.yaml")):
            relative = path.relative_to(root).as_posix()
            document = read_yaml(root, relative)
            if not isinstance(document, dict) or not isinstance(document.get(key), list):
                raise ValueError(f"{relative} requires {key} list")
            for item in document[key]:
                name = require_text(item["id"], relative + ":id")
                if name in seen:
                    raise ValueError(f"Duplicate {kind}: {name}")
                seen.add(name)
                items.append(item)
                references[name] = evidence.metadata(relative, kind, id=name)
        return items, references

    rules, rule_refs = declarations("metadata/dq_rules", "rules", "dq_rule")
    policies, policy_refs = declarations("metadata/standardization", "policies", "standardization_policy")
    if not rules:
        raise ValueError("No DQ rules declared")
    errors = validate_rule_metadata(rules, catalog, relationship_map, files)
    errors += validate_policies(policies, catalog, files)
    if errors:
        raise ValueError(f"Invalid rule/policy metadata: {errors}")
    replay_path = "metadata/replay/historical_snapshot.yaml"
    replay_document = read_yaml(root, replay_path)
    replay, _ = resolve_metadata(replay_document, catalog, files, relationships)
    replay_refs = {name: evidence.metadata(replay_path, "replay_asset", asset=name) for name in replay}
    return dict(catalog=catalog, rules=rules, policies=policies, relationships=relationships,
                replay=replay, file_refs=file_refs, asset_refs=asset_refs, column_refs=column_refs,
                rule_refs=rule_refs, policy_refs=policy_refs, relationship_refs=relationship_refs,
                replay_refs=replay_refs)


def validate_graph(nodes: dict, edges: dict, evidence: Evidence) -> None:
    for key, node in nodes.items():
        kind, zone = node["node_type"], node["zone"]
        if key != node["node_id"] or kind not in {"asset", "column", "dq_rule", "standardization_policy"}:
            raise ValueError(f"Invalid node: {key}")
        if kind in {"asset", "column"}:
            if zone not in ZONES + (("logical",) if kind == "column" else ()) or not node["asset"]:
                raise ValueError(f"Invalid node zone/asset: {key}")
            if kind == "column" and not node["column_name"]:
                raise ValueError(f"Column node lacks column: {key}")
        elif zone is not None or not node["semantic_id"]:
            raise ValueError(f"Invalid semantic node: {key}")
    for key, edge in edges.items():
        source, target, kind = edge["source_node_id"], edge["target_node_id"], edge["edge_type"]
        context = edge["context_node_id"]
        if key != edge_identifier(source, target, kind, context):
            raise ValueError(f"Invalid edge ID: {key}")
        if source not in nodes or target not in nodes:
            raise ValueError(f"Missing edge endpoint: {key}")
        if kind not in EDGE_CATEGORIES or edge["edge_category"] != EDGE_CATEGORIES[kind]:
            raise ValueError(f"Incompatible edge category/type: {key}")
        left, right = nodes[source], nodes[target]
        if kind in {"ingested_to", "published_to", "replayed_to"}:
            zones = {"ingested_to": ("source", "raw"), "published_to": ("raw", "trusted"), "replayed_to": ("trusted", "snapshot")}[kind]
            if left["node_type"] != "asset" or right["node_type"] != "asset" or (left["zone"], right["zone"]) != zones or left["asset"] != right["asset"]:
                raise ValueError(f"Invalid asset flow: {key}")
        elif kind == "transformed_by":
            if context not in nodes or nodes[context]["node_type"] != "standardization_policy":
                raise ValueError(f"Transformation requires policy context: {key}")
            if left["node_type"] != "column" or right["node_type"] != "column" or (left["zone"], right["zone"]) != ("raw", "trusted"):
                raise ValueError(f"Invalid transformation endpoints: {key}")
            scope = (left["asset"], left["column_name"])
            if scope != (right["asset"], right["column_name"]) or scope != (nodes[context]["asset"], nodes[context]["column_name"]):
                raise ValueError(f"Transformation contradicts policy scope: {key}")
        elif kind == "evaluated_by":
            if left["node_type"] != "column" or left["zone"] not in {"raw", "trusted", "snapshot"} or right["node_type"] != "dq_rule" or (left["asset"], left["column_name"]) != (right["asset"], right["column_name"]):
                raise ValueError(f"Invalid evaluation: {key}")
        elif left["node_type"] != "column" or right["node_type"] != "column" or (left["zone"], right["zone"]) != ("logical", "logical"):
            raise ValueError(f"Invalid logical relationship: {key}")
        if kind != "transformed_by" and context is not None:
            raise ValueError(f"Unexpected context: {key}")
        if not edge["evidence"]:
            raise ValueError(f"Empty edge evidence: {key}")
        for reference in edge["evidence"]:
            evidence.validate(reference)


def build_graph(config: LineageConfig) -> tuple[dict, dict]:
    root = config.root_path.resolve()
    evidence = Evidence(root)
    metadata = load_metadata(root, evidence)
    contracts = {component: evidence.contracts(component) for component in CONTRACTS}
    nodes, edges = {}, {}

    def node(kind, zone=None, asset=None, column=None, semantic=None):
        prefix = {"dq_rule": "rule", "standardization_policy": "policy"}.get(kind, kind)
        parts = [semantic] if semantic else [zone, asset] + ([column] if column else [])
        key = identifier(prefix, *parts)
        value = dict(node_id=key, node_type=kind, zone=zone, asset=asset, column_name=column, semantic_id=semantic)
        insert_unique(nodes, key, value)
        return key

    def edge(source, target, kind, refs, context=None):
        key = edge_identifier(source, target, kind, context)
        references = sorted({canonical(r): r for r in refs}.values(), key=canonical)
        value = dict(edge_id=key, source_node_id=source, target_node_id=target,
                     edge_category=EDGE_CATEGORIES[kind], edge_type=kind,
                     context_node_id=context, evidence=references)
        insert_unique(edges, key, value)

    for asset, entry in sorted(metadata["catalog"].items()):
        assets = {zone: node("asset", zone, asset) for zone in ZONES}
        refs = [metadata["asset_refs"][asset], metadata["file_refs"][entry["source_file"]]]
        edge(assets["source"], assets["raw"], "ingested_to", refs + contracts["ingestion"])
        edge(assets["raw"], assets["trusted"], "published_to", refs + contracts["standardization"])
        edge(assets["trusted"], assets["snapshot"], "replayed_to", [metadata["asset_refs"][asset], metadata["replay_refs"][asset]] + contracts["replay"])
    for rule in sorted(metadata["rules"], key=lambda r: r["id"]):
        asset, column = rule["asset"], rule["column"]
        target = node("dq_rule", asset=asset, column=column, semantic=rule["id"])
        for zone in ("raw", "trusted", "snapshot"):
            source = node("column", zone, asset, column)
            edge(source, target, "evaluated_by", [metadata["rule_refs"][rule["id"]], metadata["column_refs"][asset, column]] + contracts["dq"])
    for policy in sorted(metadata["policies"], key=lambda p: p["id"]):
        asset, column = policy["asset"], policy["column"]
        context = node("standardization_policy", asset=asset, column=column, semantic=policy["id"])
        edge(node("column", "raw", asset, column), node("column", "trusted", asset, column),
             "transformed_by", [metadata["policy_refs"][policy["id"]], metadata["column_refs"][asset, column]] + contracts["standardization"], context)
    for relationship in sorted(metadata["relationships"], key=lambda r: r["name"]):
        left, right = relationship["from"], relationship["to"]
        refs = [metadata["relationship_refs"][relationship["name"]],
                metadata["column_refs"][left["asset"], left["column"]],
                metadata["column_refs"][right["asset"], right["column"]]]
        source = node("column", "logical", left["asset"], left["column"])
        target = node("column", "logical", right["asset"], right["column"])
        key = edge_identifier(source, target, "references", None)
        if key in edges:
            refs += edges[key]["evidence"]
            del edges[key]
        edge(source, target, "references", refs)
    validate_graph(nodes, edges, evidence)
    return dict(sorted(nodes.items())), dict(sorted(edges.items()))


def publish_graph(config: LineageConfig, nodes: dict, edges: dict) -> None:
    database = config.database_path.resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    staging = database.with_name(f".{database.name}.staging-{uuid.uuid4().hex}")
    connection = None
    published = False
    try:
        connection = duckdb.connect(str(staging))
        connection.execute("""CREATE TABLE lineage_nodes (
            node_id VARCHAR PRIMARY KEY, node_type VARCHAR NOT NULL,
            zone VARCHAR, asset VARCHAR, column_name VARCHAR, semantic_id VARCHAR,
            CHECK (node_type IN ('asset','column','dq_rule','standardization_policy'))
        )""")
        connection.execute("""CREATE TABLE lineage_edges (
            edge_id VARCHAR PRIMARY KEY,
            source_node_id VARCHAR NOT NULL REFERENCES lineage_nodes(node_id),
            target_node_id VARCHAR NOT NULL REFERENCES lineage_nodes(node_id),
            edge_category VARCHAR NOT NULL, edge_type VARCHAR NOT NULL,
            context_node_id VARCHAR REFERENCES lineage_nodes(node_id), evidence JSON NOT NULL,
            CHECK ((edge_category='DATA_FLOW' AND edge_type IN ('ingested_to','published_to','replayed_to'))
                OR (edge_category='TRANSFORMATION' AND edge_type='transformed_by')
                OR (edge_category='EVALUATION' AND edge_type='evaluated_by')
                OR (edge_category='DATA_RELATIONSHIP' AND edge_type='references')),
            CHECK ((edge_type='transformed_by' AND context_node_id IS NOT NULL)
                OR (edge_type<>'transformed_by' AND context_node_id IS NULL))
        )""")
        connection.executemany("INSERT INTO lineage_nodes VALUES (?,?,?,?,?,?)", [
            [n[k] for k in ("node_id", "node_type", "zone", "asset", "column_name", "semantic_id")]
            for n in nodes.values()
        ])
        connection.executemany("INSERT INTO lineage_edges VALUES (?,?,?,?,?,?,?)", [
            [e[k] for k in ("edge_id", "source_node_id", "target_node_id", "edge_category", "edge_type", "context_node_id")] + [canonical(e["evidence"])]
            for e in edges.values()
        ])
        counts = connection.execute("SELECT (SELECT count(*) FROM lineage_nodes), (SELECT count(*) FROM lineage_edges)").fetchone()
        if counts != (len(nodes), len(edges)):
            raise ValueError("Staging graph count mismatch")
        connection.execute("CHECKPOINT")
        connection.close()
        connection = None
        os.replace(staging, database)
        published = True
    finally:
        if connection is not None:
            connection.close()
        for path in (staging, staging.with_name(staging.name + ".wal")):
            try:
                if path.exists():
                    path.unlink()
            except Exception:
                if not published:
                    raise


def write_build_record(record: dict, runs_path: Path) -> Path:
    runs_path.mkdir(parents=True, exist_ok=True)
    path = runs_path / (record["build_run_id"] + ".json")
    temporary = path.with_name("." + path.name + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as output:
            json.dump(record, output, indent=2, ensure_ascii=False)
            output.write("\n")
            output.flush()
        os.replace(temporary, path)
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except Exception:
            pass
    return path


def run_lineage(config: LineageConfig) -> dict:
    started = time.perf_counter()
    record = dict(build_run_id="lineage-" + uuid.uuid4().hex, status="FAILED",
                  nodes_generated=0, edges_generated=0, errors=[])
    try:
        nodes, edges = build_graph(config)
        record["nodes_generated"], record["edges_generated"] = len(nodes), len(edges)
        publish_graph(config, nodes, edges)
        record["status"] = "SUCCESS"
    except Exception as exc:
        record["errors"].append(f"{type(exc).__name__}: {exc}")
    record["completed_at"] = datetime.now(timezone.utc).isoformat()
    record["elapsed_seconds"] = round(time.perf_counter() - started, 6)
    try:
        path = write_build_record(record, config.runs_path)
        record["execution_record"] = str(path.resolve())
    except Exception as exc:
        record["errors"].append(f"Could not write build record: {exc}")
        record["execution_record"] = "UNAVAILABLE"
        # The optional record is secondary evidence; its failure cannot change
        # the status of an already atomically published graph.
    return record
