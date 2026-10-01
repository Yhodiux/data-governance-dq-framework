"""Validate explicit governance metadata and resolve original evidence."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PureWindowsPath

import duckdb
import yaml

ISSUE_STATUSES = {"OPEN", "RESOLVED"}
DECISION_TYPES = {"REMEDIATION_AUTHORIZED", "REMEDIATION_WITHHELD"}
DECISION_STATUSES = {"EFFECTIVE", "ACTIVE"}
EVIDENCE_TYPES = {"METADATA", "DQ_EXECUTION", "STANDARDIZATION_EXECUTION"}


@dataclass(frozen=True)
class GovernanceConfig:
    root_path: Path

    @property
    def database_path(self):
        return self.root_path / "data/results/governance/governance_registry.duckdb"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def unique_mapping(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate key: {key}")
        result[key] = value
    return result


class UniqueLoader(yaml.SafeLoader):
    pass


def yaml_mapping(loader, node):
    loader.flatten_mapping(node)
    return unique_mapping((loader.construct_object(k), loader.construct_object(v)) for k, v in node.value)


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, yaml_mapping)


def load_yaml(path):
    return yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)


def text(value, label, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be nonempty text")
    return value


def enum(value, allowed, label):
    if value not in allowed:
        raise ValueError(f"Unsupported {label}: {value}")
    return value


def declarations(root, directory, key):
    items = {}
    for path in sorted((root / directory).glob("*.yaml")):
        document = load_yaml(path)
        if not isinstance(document, dict) or not isinstance(document.get(key), list):
            raise ValueError(f"{path}: {key} must be a list")
        for item in document[key]:
            if not isinstance(item, dict):
                raise ValueError("Declaration must be a mapping")
            identifier = text(item.get("id"), "metadata id")
            if identifier in items:
                raise ValueError(f"Duplicate metadata ID: {identifier}")
            text(item.get("asset"), "metadata asset")
            text(item.get("column"), "metadata column")
            items[identifier] = (item, path.relative_to(root).as_posix())
    return items


def check_policy(policy_id, issue, policies):
    if policy_id not in policies:
        raise ValueError(f"Unknown policy: {policy_id}")
    policy = policies[policy_id][0]
    if (policy["asset"], policy["column"]) != (issue["asset"], issue["column"]):
        raise ValueError("Policy scope conflicts with issue")


def resolve_evidence(root, reference, issue, rules, policies):
    if not isinstance(reference, dict):
        raise ValueError("Evidence must be a mapping")
    kind = enum(reference.get("type"), EVIDENCE_TYPES, "evidence type")
    relative = text(reference.get("path"), "evidence path")
    path = Path(relative)
    if path.is_absolute() or PureWindowsPath(relative).is_absolute() or ".." in path.parts or "\\" in relative:
        raise ValueError("Evidence path must be a relative POSIX path within the project")
    full = (root / path).resolve()
    if root not in full.parents or not full.is_file():
        raise ValueError(f"Evidence path unavailable: {relative}")
    run = text(reference.get("run_id"), "evidence run_id", True)
    rule = text(reference.get("rule_id"), "evidence rule_id", True)
    policy = text(reference.get("policy_id"), "evidence policy_id", True)
    description = text(reference.get("description"), "evidence description")
    if rule is not None and rule != issue["rule_id"]:
        raise ValueError("Evidence rule conflicts with issue")
    if policy is not None:
        check_policy(policy, issue, policies)
    if kind == "METADATA":
        if run is not None:
            raise ValueError("Metadata evidence cannot reference a run")
        if rule is not None and policy is not None:
            raise ValueError("Metadata evidence must select one declaration")
        if rule is not None:
            if relative != rules[rule][1]:
                raise ValueError("Metadata evidence rule/path mismatch")
        elif policy is not None:
            if relative != policies[policy][1]:
                raise ValueError("Metadata evidence policy/path mismatch")
        else:
            if path.parent.as_posix() != "metadata/catalog" or path.suffix != ".yaml":
                raise ValueError("Metadata evidence without ID must select the issue catalog scope")
            catalog = load_yaml(full)
            if catalog["asset"]["name"] != issue["asset"] or not any(c["name"] == issue["column"] for c in catalog["columns"]):
                raise ValueError("Catalog evidence scope conflicts with issue")
    else:
        process = "dq" if kind == "DQ_EXECUTION" else "standardization"
        if path.parent.as_posix() != "data/results/" + process or path.suffix != ".json":
            raise ValueError("Execution evidence must reference an original process JSON")
        text(run, "execution evidence run_id")
        document = json.loads(full.read_text(encoding="utf-8"), object_pairs_hook=unique_mapping)
        if not isinstance(document, dict) or document.get("run_id") != run or path.name != run + ".json":
            raise ValueError("Execution evidence run identity mismatch")
        key, semantic_key, semantic = ("rule_results", "rule_id", rule) if process == "dq" else ("policy_results", "policy_id", policy)
        text(semantic, "execution evidence semantic ID")
        if (process == "dq" and policy is not None) or (process == "standardization" and rule is not None):
            raise ValueError("Execution evidence contains an incompatible selector")
        if not isinstance(document.get(key), list):
            raise ValueError("Invalid execution evidence detail structure")
        matches = [r for r in document[key] if isinstance(r, dict) and r.get(semantic_key) == semantic]
        if len(matches) != 1 or (matches[0].get("asset"), matches[0].get("column")) != (issue["asset"], issue["column"]):
            raise ValueError("Execution evidence semantic identity/scope mismatch")
    return kind, relative, run, rule, policy, description


def load_registry(config):
    root = config.root_path.resolve()
    rules = declarations(root, "metadata/dq_rules", "rules")
    policies = declarations(root, "metadata/standardization", "policies")
    issue_doc = load_yaml(root / "metadata/governance/issues.yaml")
    decision_doc = load_yaml(root / "metadata/governance/decisions.yaml")
    if not isinstance(issue_doc, dict) or not isinstance(issue_doc.get("issues"), list):
        raise ValueError("issues must be a list")
    if not isinstance(decision_doc, dict) or not isinstance(decision_doc.get("decisions"), list):
        raise ValueError("decisions must be a list")
    issues, decisions, evidence = {}, {}, {}

    def add_evidence(parent_type, parent_id, parent, issue):
        references = parent.get("evidence", [])
        if not isinstance(references, list):
            raise ValueError("evidence must be a list")
        for reference in references:
            fields = resolve_evidence(root, reference, issue, rules, policies)
            identifier = "evidence:" + hashlib.sha256(canonical([parent_type, parent_id, *fields[:5]]).encode("utf-8")).hexdigest()
            if identifier in evidence:
                raise ValueError("Duplicate evidence ID")
            evidence[identifier] = (identifier, parent_type, parent_id, *fields)

    for issue in issue_doc["issues"]:
        if not isinstance(issue, dict):
            raise ValueError("Issue must be a mapping")
        identifier = text(issue.get("issue_id"), "issue_id")
        if identifier in issues:
            raise ValueError("Duplicate issue_id")
        asset, column, rule = (text(issue.get(k), k) for k in ("asset", "column", "rule_id"))
        if rule not in rules:
            raise ValueError(f"Unknown rule: {rule}")
        declaration = rules[rule][0]
        if (asset, column) != (declaration["asset"], declaration["column"]):
            raise ValueError("Rule scope conflicts with issue")
        status = enum(issue.get("status"), ISSUE_STATUSES, "issue status")
        description = text(issue.get("description"), "issue description")
        issues[identifier] = (identifier, asset, column, rule, status, description)
        add_evidence("ISSUE", identifier, issue, issue)
    issue_map = {i["issue_id"]: i for i in issue_doc["issues"]}
    for decision in decision_doc["decisions"]:
        if not isinstance(decision, dict):
            raise ValueError("Decision must be a mapping")
        identifier = text(decision.get("decision_id"), "decision_id")
        if identifier in decisions:
            raise ValueError("Duplicate decision_id")
        issue_id = text(decision.get("issue_id"), "decision issue_id")
        if issue_id not in issues:
            raise ValueError("Decision references unknown issue")
        kind = enum(decision.get("decision_type"), DECISION_TYPES, "decision type")
        status = enum(decision.get("status"), DECISION_STATUSES, "decision status")
        policy = text(decision.get("policy_id"), "policy_id", True)
        if kind == "REMEDIATION_AUTHORIZED" and policy is None:
            raise ValueError("REMEDIATION_AUTHORIZED requires policy_id")
        if policy is not None:
            check_policy(policy, issue_map[issue_id], policies)
        actor = text(decision.get("actor"), "actor", True)
        decision_date = decision.get("decision_date")
        if decision_date is not None:
            value = decision_date.isoformat() if type(decision_date) is date else text(decision_date, "decision_date")
            decision_date = date.fromisoformat(value)
            if decision_date.isoformat() != value:
                raise ValueError("decision_date must use ISO YYYY-MM-DD")
        rationale = text(decision.get("rationale"), "rationale")
        decisions[identifier] = (identifier, issue_id, kind, status, policy, decision_date, actor, rationale)
        add_evidence("DECISION", identifier, decision, issue_map[issue_id])
    for identifier, issue in issues.items():
        if issue[4] == "RESOLVED" and not any(d[1] == identifier and d[3] == "EFFECTIVE" for d in decisions.values()):
            raise ValueError("RESOLVED issue requires an EFFECTIVE decision")
    return tuple([mapping[k] for k in sorted(mapping)] for mapping in (issues, decisions, evidence))


def publish_registry(config, rows):
    database = config.database_path.resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    staging = database.with_name("." + database.name + ".staging")
    connection, published = None, False
    try:
        connection = duckdb.connect(str(staging))
        connection.execute("""CREATE TABLE governance_issues (
            issue_id VARCHAR PRIMARY KEY, asset VARCHAR NOT NULL, column_name VARCHAR NOT NULL,
            rule_id VARCHAR NOT NULL, status VARCHAR NOT NULL, description VARCHAR NOT NULL,
            CHECK(status IN ('OPEN','RESOLVED')))""")
        connection.execute("""CREATE TABLE governance_decisions (
            decision_id VARCHAR PRIMARY KEY, issue_id VARCHAR NOT NULL REFERENCES governance_issues(issue_id),
            decision_type VARCHAR NOT NULL, status VARCHAR NOT NULL, policy_id VARCHAR,
            decision_date DATE, actor VARCHAR, rationale VARCHAR NOT NULL,
            CHECK(decision_type IN ('REMEDIATION_AUTHORIZED','REMEDIATION_WITHHELD')),
            CHECK(status IN ('EFFECTIVE','ACTIVE')),
            CHECK(decision_type<>'REMEDIATION_AUTHORIZED' OR policy_id IS NOT NULL))""")
        connection.execute("""CREATE TABLE governance_evidence (
            evidence_id VARCHAR PRIMARY KEY, parent_type VARCHAR NOT NULL, parent_id VARCHAR NOT NULL,
            evidence_type VARCHAR NOT NULL, path VARCHAR NOT NULL, run_id VARCHAR, rule_id VARCHAR,
            policy_id VARCHAR, description VARCHAR NOT NULL,
            CHECK(parent_type IN ('ISSUE','DECISION')),
            CHECK(evidence_type IN ('METADATA','DQ_EXECUTION','STANDARDIZATION_EXECUTION')))""")
        for table, width, values in zip(("governance_issues", "governance_decisions", "governance_evidence"), (6, 8, 9), rows):
            if values:
                connection.executemany(f"INSERT INTO {table} VALUES ({','.join('?' for _ in range(width))})", values)
            if connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] != len(values):
                raise ValueError("Staging count mismatch")
        invalid = connection.execute("""SELECT count(*) FROM governance_evidence e
            LEFT JOIN governance_issues i ON e.parent_type='ISSUE' AND e.parent_id=i.issue_id
            LEFT JOIN governance_decisions d ON e.parent_type='DECISION' AND e.parent_id=d.decision_id
            WHERE i.issue_id IS NULL AND d.decision_id IS NULL""").fetchone()[0]
        if invalid:
            raise ValueError("Evidence references unknown parent")
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


def run_governance(config):
    result = dict(status="FAILED", issues=0, decisions=0, evidence=0,
                  database_path=str(config.database_path.resolve()), errors=[])
    try:
        rows = load_registry(config)
        result.update(zip(("issues", "decisions", "evidence"), map(len, rows)))
        publish_registry(config, rows)
        result["status"] = "SUCCESS"
    except Exception as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    return result
