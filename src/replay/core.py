"""Select original physical lines using explicit temporal and reference metadata."""

from __future__ import annotations

import csv
import json
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.ingestion.core import load_manifest
from src.standardization.core import (
    load_catalog, load_physical_format, load_yaml, sha256,
    split_line_ending, split_physical_fields,
)


@dataclass(frozen=True)
class ReplayConfig:
    manifest_path: Path
    catalog_path: Path
    relationships_path: Path
    policy_path: Path
    source_path: Path
    snapshots_path: Path
    results_path: Path
    cutoff: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_cutoff(value: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("cutoff must use ISO YYYY-MM-DD")
    return date.fromisoformat(value)


def parse_temporal(value: str, temporal: dict[str, Any]) -> date:
    if not re.fullmatch(r"[0-9]{6}", value):
        raise ValueError(f"Invalid YYMMDD date: {value!r}")
    return date(temporal["century"] + int(value[:2]), int(value[2:4]), int(value[4:]))


def resolve_metadata(document: Any, catalog: dict, files: set[str], relationships: list) -> tuple[dict, list[str]]:
    if not isinstance(document, dict) or not isinstance(document.get("assets"), dict):
        raise ValueError("Replay metadata must define an assets mapping")
    policies = document["assets"]
    if set(policies) != set(catalog):
        raise ValueError("Replay assets must match catalog assets exactly (unknown or missing asset)")
    if {a["source_file"] for a in catalog.values()} != files or len(catalog) != len(files):
        raise ValueError("Catalog must cover manifest files exactly")
    for name, policy in policies.items():
        if not isinstance(policy, dict):
            raise ValueError(f"{name}: policy must be a mapping")
        strategy = policy.get("strategy")
        if strategy not in {"temporal", "reference", "temporal_and_reference", "static"}:
            raise ValueError(f"{name}: invalid strategy {strategy}")
        temporal = policy.get("temporal")
        references = policy.get("references", [])
        if strategy in {"temporal", "temporal_and_reference"}:
            if not isinstance(temporal, dict) or temporal.get("format") != "YYMMDD" or type(temporal.get("century")) is not int or temporal["century"] != 1900:
                raise ValueError(f"{name}: temporal metadata requires format YYMMDD and century 1900")
            column = temporal.get("column")
            if column not in catalog[name]["columns"]:
                raise ValueError(f"{name}: unknown temporal column {column}")
            if catalog[name]["columns"][column].get("documented_format") != "YYMMDD":
                raise ValueError(f"{name}: temporal column lacks documented YYMMDD evidence")
        elif temporal is not None:
            raise ValueError(f"{name}: strategy does not accept temporal metadata")
        if not isinstance(references, list) or (strategy in {"reference", "temporal_and_reference"} and not references):
            raise ValueError(f"{name}: references must be a nonempty list for reference strategies")
        if strategy in {"static", "temporal"} and references:
            raise ValueError(f"{name}: strategy does not accept references")
        for ref in references:
            if not isinstance(ref, dict):
                raise ValueError(f"{name}: invalid reference")
            target = ref.get("asset")
            column, target_column = ref.get("column"), ref.get("target_column")
            if target not in policies:
                raise ValueError(f"{name}: unknown reference asset {target}")
            if column not in catalog[name]["columns"] or target_column not in catalog[target]["columns"]:
                raise ValueError(f"{name}: unknown reference column")
            endpoints = ({"asset": name, "column": column}, {"asset": target, "column": target_column})
            matches = [r for r in relationships if r.get("name") == ref.get("relationship")]
            if len(matches) != 1 or (matches[0]["from"], matches[0]["to"]) not in (endpoints, endpoints[::-1]):
                raise ValueError(f"{name}: invalid source-supported reference {ref}")
    order, visiting, visited = [], set(), set()

    def visit(name: str) -> None:
        if name in visiting:
            raise ValueError(f"Replay dependency cycle at {name}")
        if name in visited:
            return
        visiting.add(name)
        for ref in policies[name].get("references", []):
            visit(ref["asset"])
        visiting.remove(name)
        visited.add(name)
        order.append(name)

    for name in sorted(policies):
        visit(name)
    return policies, order


def physical_rows(path: Path, expected: list[str], delimiter: str, quote: str):
    """One record per physical line; retain original bytes, including CRLF/BOM."""
    with path.open("rb") as source:
        header = source.readline()
        if not header:
            raise ValueError(f"{path.name}: missing header")
        decoded = header.decode("utf-8-sig")
        names = next(csv.reader([split_line_ending(decoded)[0]], delimiter=delimiter, quotechar=quote, strict=True))
        if names != expected:
            raise ValueError(f"{path.name}: header differs from catalog")
        yield header, None
        for number, line in enumerate(source, 2):
            try:
                text = split_line_ending(line.decode("utf-8"))[0]
                tokens = split_physical_fields(text, delimiter, quote)
                values = next(csv.reader([text], delimiter=delimiter, quotechar=quote, strict=True))
                if len(values) != len(names) or len(tokens) != len(names):
                    raise ValueError("unexpected column count")
                yield line, dict(zip(names, values))
            except (ValueError, csv.Error) as exc:
                raise ValueError(f"{path.name} line {number}: {exc}") from exc


def scan(path: Path, catalog: dict, policies: dict, delimiter: str, quote: str, cutoff: date | None = None) -> tuple[dict, dict]:
    keys = {name: {column: set() for column in entry["columns"] if any(
        (r["asset"] == name and r["target_column"] == column) or (owner == name and r["column"] == column)
        for owner, policy in policies.items() for r in policy.get("references", [])
    )} for name, entry in catalog.items()}
    counts = {}
    for name, entry in catalog.items():
        count = 0
        for _, row in physical_rows(path / entry["source_file"], list(entry["columns"]), delimiter, quote):
            if row is None:
                continue
            count += 1
            temporal = policies[name].get("temporal")
            if temporal:
                try:
                    value = parse_temporal(row[temporal["column"]], temporal)
                except ValueError as exc:
                    raise ValueError(f"{name} row {count}: {exc}") from exc
                if cutoff is not None and value > cutoff:
                    raise ValueError(f"{name}: staged future date")
            for column, values in keys[name].items():
                values.add(row[column])
        counts[name] = count
    return keys, counts


def validate_source_references(keys: dict, policies: dict, relationships: list) -> None:
    used = {ref["relationship"] for p in policies.values() for ref in p.get("references", [])}
    for relationship in relationships:
        if relationship["name"] not in used:
            continue
        left, right = relationship["from"], relationship["to"]
        source = keys[left["asset"]][left["column"]]
        target = keys[right["asset"]][right["column"]]
        missing = source - target
        if "" in source or missing:
            raise ValueError(f"Orphan/unresolved reference {relationship['name']}: {sorted(missing)[:5]}")


def write_execution_record(record: dict, results_path: Path) -> str:
    results_path.mkdir(parents=True, exist_ok=True)
    path = results_path / (record["run_id"] + ".json")
    temporary = results_path / ("." + record["run_id"] + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as output:
            json.dump(record, output, ensure_ascii=False, indent=2)
            output.write("\n")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return str(path.resolve())


def run_replay(config: ReplayConfig) -> dict[str, Any]:
    started = time.perf_counter()
    token = uuid.uuid4().hex
    record = dict(run_id="replay-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + token[:8],
                  started_at=utc_now(), completed_at=None, elapsed_seconds=None, status="FAILED",
                  cutoff=config.cutoff, source_zone="trusted", source_path=str(config.source_path.resolve()),
                  snapshot_path=None, metadata={}, assets=[], errors=[])
    staging = backup = snapshot = None
    moved = False
    published = False
    written = False
    safe_results = False
    try:
        source = config.source_path.resolve()
        results = config.results_path.resolve()
        if results == source or source in results.parents or results in source.parents:
            raise ValueError("Input and execution record directories must not overlap")
        safe_results = True
        cutoff = parse_cutoff(config.cutoff)
        snapshot = config.snapshots_path.resolve() / cutoff.isoformat()
        record["snapshot_path"] = str(snapshot)
        for output in (snapshot.parent, results):
            if output == source or source in output.parents or output in source.parents:
                raise ValueError("Input and output directories must not overlap")
        catalog = load_catalog(config.catalog_path)
        files = {entry["name"] for entry in load_manifest(config.manifest_path)}
        if any(Path(f).name != f or f in {".", ".."} for f in files):
            raise ValueError("Manifest files must be plain file names")
        delimiter, quote = load_physical_format(config.manifest_path)
        document = load_yaml(config.policy_path)
        relationships = load_yaml(config.relationships_path)["relationships"]
        record["metadata"] = {"policy_path": str(config.policy_path.resolve()), "policy": document,
                              "relationships_path": str(config.relationships_path.resolve()), "relationships": relationships}
        policies, order = resolve_metadata(document, catalog, files, relationships)
        fingerprints = {f: sha256(source / f) for f in files}
        keys, input_counts = scan(source, catalog, policies, delimiter, quote)
        validate_source_references(keys, policies, relationships)
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        staging = snapshot.parent / f".{snapshot.name}-staging-{token}"
        backup = snapshot.parent / f".{snapshot.name}-backup-{token}"
        staging.mkdir()
        selected = {}
        for name in order:
            entry, policy = catalog[name], policies[name]
            selected[name] = {column: set() for column in keys[name]}
            output_rows = 0
            target = staging / entry["source_file"]
            with target.open("wb") as destination:
                for line, row in physical_rows(source / entry["source_file"], list(entry["columns"]), delimiter, quote):
                    if row is None:
                        destination.write(line)
                        continue
                    temporal = policy.get("temporal")
                    keep = not temporal or parse_temporal(row[temporal["column"]], temporal) <= cutoff
                    keep = keep and all(row[r["column"]] in selected[r["asset"]][r["target_column"]] for r in policy.get("references", []))
                    if keep:
                        destination.write(line)
                        output_rows += 1
                        for column, values in selected[name].items():
                            values.add(row[column])
            record["assets"].append(dict(asset=name, strategy=policy["strategy"], input_rows=input_counts[name], output_rows=output_rows))
        if {p.name for p in staging.iterdir()} != files:
            raise ValueError("Staging does not contain exactly the manifest assets")
        staged_keys, staged_counts = scan(staging, catalog, policies, delimiter, quote, cutoff)
        validate_source_references(staged_keys, policies, relationships)
        for result in record["assets"]:
            name = result["asset"]
            if staged_counts[name] != result["output_rows"]:
                raise ValueError(f"{name}: staging count mismatch")
            if policies[name]["strategy"] == "static" and sha256(staging / catalog[name]["source_file"]) != fingerprints[catalog[name]["source_file"]]:
                raise ValueError(f"{name}: static bytes changed")
        if fingerprints != {f: sha256(source / f) for f in files}:
            raise ValueError("TRUSTED changed during construction")
        if snapshot.exists():
            snapshot.replace(backup)
            moved = True
        staging.replace(snapshot)
        published = True
        record["status"] = "SUCCESS"
        record["completed_at"] = utc_now()
        record["elapsed_seconds"] = round(time.perf_counter() - started, 6)
        record["execution_record"] = write_execution_record(record, results)
        written = True
    except Exception as exc:
        record["status"] = "FAILED"
        record["errors"].append(f"{type(exc).__name__}: {exc}")
        if moved and backup is not None and backup.exists():
            if snapshot.exists():
                shutil.rmtree(snapshot)
            backup.replace(snapshot)
        elif published and snapshot is not None and snapshot.exists():
            # Publication succeeded but evidence writing failed on a first build.
            shutil.rmtree(snapshot)
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
        if record["status"] == "SUCCESS" and backup is not None and backup.exists():
            shutil.rmtree(backup)
        if not written:
            record["completed_at"] = utc_now()
            record["elapsed_seconds"] = round(time.perf_counter() - started, 6)
            record["execution_record"] = "UNAVAILABLE"
            if safe_results:
                try:
                    record["execution_record"] = write_execution_record(record, config.results_path)
                except Exception as exc:
                    record["errors"].append(f"Could not write execution record: {exc}")
    return record
