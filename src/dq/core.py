"""Generic DuckDB execution of metadata-declared Data Quality rules."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import yaml

from src.ingestion.core import load_manifest

LOGGER = logging.getLogger(__name__)
SUPPORTED_DIMENSIONS = {"validity", "uniqueness", "referential_integrity"}
SUPPORTED_EXPECTATIONS = {
    "allowed_values",
    "regex_format",
    "unique",
    "reference_exists",
}
DIMENSION_EXPECTATIONS = {
    "validity": {"allowed_values", "regex_format"},
    "uniqueness": {"unique"},
    "referential_integrity": {"reference_exists"},
}
EMPTY_POLICIES = {"evaluate", "ignore"}
SAMPLE_SIZE = 5
DATA_ZONES = {"raw", "trusted"}


@dataclass(frozen=True)
class DQConfig:
    manifest_path: Path
    catalog_path: Path
    relationships_path: Path
    rules_path: Path
    data_path: Path
    data_zone: str
    results_path: Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as yaml_file:
        return yaml.safe_load(yaml_file)


def load_catalog(catalog_path: Path) -> dict[str, dict[str, Any]]:
    catalog: dict[str, dict[str, Any]] = {}
    for path in sorted(catalog_path.glob("*.yaml")):
        document = load_yaml(path)
        asset = document["asset"]
        catalog[asset["name"]] = {
            "source_file": asset["source_file"],
            "columns": {
                column["name"]: column for column in document["columns"]
            },
        }
    return catalog


def load_relationships(path: Path) -> dict[str, dict[str, Any]]:
    document = load_yaml(path)
    return {
        relationship["name"]: relationship
        for relationship in document["relationships"]
    }


def load_rules(rules_path: Path) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    for path in sorted(rules_path.glob("*.yaml")):
        document = load_yaml(path)
        if not isinstance(document, dict) or not isinstance(document.get("rules"), list):
            raise ValueError(f"{path.name} must define a rules list")
        rules.extend(document["rules"])
    if not rules:
        raise ValueError("No DQ rules were found")
    return rules


def load_physical_format(manifest_path: Path) -> tuple[str, str]:
    manifest = load_yaml(manifest_path)
    try:
        physical_format = manifest["dataset"]["physical_format"]
        delimiter = str(physical_format["delimiter"])
        quote = str(physical_format["text_qualifier"])
        has_header = physical_format["header"]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            "Manifest must define physical_format delimiter, text_qualifier, and header"
        ) from exc
    if len(delimiter.encode("utf-8")) != 1 or len(quote.encode("utf-8")) != 1:
        raise ValueError("Delimiter and text qualifier must each be one byte")
    if has_header is not True:
        raise ValueError("DQ execution requires the manifest header setting to be true")
    return delimiter, quote


def metadata_error(rule_id: Any, message: str) -> dict[str, str]:
    return {"rule_id": str(rule_id) if rule_id else "<unknown>", "message": message}


def validate_rule_metadata(
    rules: list[dict[str, Any]],
    catalog: dict[str, dict[str, Any]],
    relationships: dict[str, dict[str, Any]],
    manifest_files: set[str],
) -> list[dict[str, str]]:
    """Validate rule structure, references, operators, and evidence alignment."""

    errors: list[dict[str, str]] = []
    rule_ids = [rule.get("id") for rule in rules if isinstance(rule, dict)]
    for rule_id, count in Counter(rule_ids).items():
        if rule_id and count > 1:
            errors.append(metadata_error(rule_id, "Rule ID is duplicated"))

    required_fields = (
        "id",
        "asset",
        "column",
        "dimension",
        "expectation",
        "empty_policy",
        "evidence",
    )
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            errors.append(metadata_error(None, f"Rule {index} must be a mapping"))
            continue
        rule_id = rule.get("id")
        missing = [field for field in required_fields if field not in rule]
        if missing:
            errors.append(metadata_error(rule_id, f"Missing required fields: {', '.join(missing)}"))
            continue
        if not isinstance(rule_id, str) or not rule_id:
            errors.append(metadata_error(rule_id, "Rule ID must be non-empty text"))

        asset_name = rule["asset"]
        column_name = rule["column"]
        asset = catalog.get(asset_name)
        if asset is None:
            errors.append(metadata_error(rule_id, f"Unknown catalog asset: {asset_name}"))
        else:
            if asset["source_file"] not in manifest_files:
                errors.append(
                    metadata_error(
                        rule_id,
                        f"Catalog source_file is not declared in manifest: {asset['source_file']}",
                    )
                )
            if column_name not in asset["columns"]:
                errors.append(
                    metadata_error(rule_id, f"Unknown catalog column: {asset_name}.{column_name}")
                )

        dimension = rule["dimension"]
        if dimension not in SUPPORTED_DIMENSIONS:
            errors.append(metadata_error(rule_id, f"Unsupported dimension: {dimension}"))
        empty_policy = rule["empty_policy"]
        if empty_policy not in EMPTY_POLICIES:
            errors.append(metadata_error(rule_id, f"Invalid empty_policy: {empty_policy}"))

        expectation = rule["expectation"]
        if not isinstance(expectation, dict):
            errors.append(metadata_error(rule_id, "expectation must be a mapping"))
            continue
        expectation_type = expectation.get("type")
        if expectation_type not in SUPPORTED_EXPECTATIONS:
            errors.append(
                metadata_error(rule_id, f"Unsupported expectation type: {expectation_type}")
            )
            continue
        if dimension in DIMENSION_EXPECTATIONS and expectation_type not in DIMENSION_EXPECTATIONS[dimension]:
            errors.append(
                metadata_error(
                    rule_id,
                    f"Expectation {expectation_type} is not supported for dimension {dimension}",
                )
            )

        if expectation_type == "allowed_values":
            values = expectation.get("values")
            if not isinstance(values, list) or not values or any(
                not isinstance(value, str) for value in values
            ):
                errors.append(metadata_error(rule_id, "allowed_values requires a non-empty text values list"))
            elif len(values) != len(set(values)):
                errors.append(metadata_error(rule_id, "allowed_values contains duplicate values"))
        elif expectation_type == "regex_format":
            pattern = expectation.get("pattern")
            if not isinstance(pattern, str) or not pattern:
                errors.append(metadata_error(rule_id, "regex_format requires a non-empty pattern"))
            else:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    errors.append(metadata_error(rule_id, f"Invalid regex pattern: {exc}"))
        elif expectation_type == "unique":
            if set(expectation) != {"type"}:
                errors.append(metadata_error(rule_id, "unique expectation accepts only the type field"))
        elif expectation_type == "reference_exists":
            reference = expectation.get("reference")
            if not isinstance(reference, dict) or set(reference) != {"asset", "column"}:
                errors.append(
                    metadata_error(
                        rule_id,
                        "reference_exists requires reference.asset and reference.column",
                    )
                )
            else:
                target_asset = catalog.get(reference["asset"])
                if target_asset is None:
                    errors.append(
                        metadata_error(rule_id, f"Unknown reference asset: {reference['asset']}")
                    )
                elif reference["column"] not in target_asset["columns"]:
                    errors.append(
                        metadata_error(
                            rule_id,
                            f"Unknown reference column: {reference['asset']}.{reference['column']}",
                        )
                    )

        evidence = rule["evidence"]
        if not isinstance(evidence, dict) or not evidence.get("type"):
            errors.append(metadata_error(rule_id, "evidence must declare a type"))
            continue
        evidence_type = evidence["type"]
        if evidence_type == "catalog":
            expected_reference = f"{asset_name}.{column_name}"
            if evidence.get("reference") != expected_reference:
                errors.append(
                    metadata_error(rule_id, f"Catalog evidence must reference {expected_reference}")
                )
            if asset is not None and column_name in asset["columns"]:
                catalog_column = asset["columns"][column_name]
                if expectation_type == "allowed_values":
                    documented_values = catalog_column.get("documented_values")
                    if not isinstance(documented_values, dict):
                        errors.append(metadata_error(rule_id, "Catalog has no documented_values for this rule"))
                    elif set(expectation.get("values", [])) != set(documented_values):
                        errors.append(metadata_error(rule_id, "Rule values contradict catalog documented_values"))
                if expectation_type == "regex_format" and not catalog_column.get("documented_format"):
                    errors.append(metadata_error(rule_id, "Catalog has no documented_format for this rule"))
        elif evidence_type == "catalog_relationship":
            relationship_name = evidence.get("reference")
            relationship = relationships.get(relationship_name)
            if relationship is None:
                errors.append(
                    metadata_error(rule_id, f"Unknown catalog relationship: {relationship_name}")
                )
            elif expectation_type == "reference_exists":
                reference = expectation.get("reference", {})
                if relationship.get("from") != {"asset": asset_name, "column": column_name}:
                    errors.append(metadata_error(rule_id, "Rule source contradicts catalog relationship"))
                if relationship.get("to") != reference:
                    errors.append(metadata_error(rule_id, "Rule target contradicts catalog relationship"))
        elif evidence_type == "source_documentation":
            if not isinstance(evidence.get("description"), str) or not evidence["description"]:
                errors.append(metadata_error(rule_id, "Source-documentation evidence requires a description"))
        else:
            errors.append(metadata_error(rule_id, f"Unsupported evidence type: {evidence_type}"))
    return errors


def load_raw_tables(
    connection: duckdb.DuckDBPyConnection,
    catalog: dict[str, dict[str, Any]],
    rules: list[dict[str, Any]],
    data_path: Path,
    delimiter: str,
    quote: str,
) -> dict[str, str]:
    asset_names = {rule["asset"] for rule in rules}
    for rule in rules:
        if rule["expectation"]["type"] == "reference_exists":
            asset_names.add(rule["expectation"]["reference"]["asset"])

    table_names: dict[str, str] = {}
    for index, asset_name in enumerate(sorted(asset_names)):
        data_file = data_path / catalog[asset_name]["source_file"]
        if not data_file.is_file():
            raise FileNotFoundError(f"Data-zone file is missing: {data_file.name}")
        table_name = f"dq_asset_{index}"
        connection.execute(
            f"""
            CREATE TEMP TABLE {quote_identifier(table_name)} AS
            SELECT * FROM read_csv(
                ?, header = true, delim = ?, quote = ?,
                all_varchar = true, nullstr = chr(0)
            )
            """,
            [str(data_file), delimiter, quote],
        )
        table_names[asset_name] = table_name
    return table_names


def sample_text(value: Any) -> str:
    return "<PARSER_NULL>" if value is None else str(value)


def execute_rule(
    connection: duckdb.DuckDBPyConnection,
    rule: dict[str, Any],
    table_names: dict[str, str],
) -> dict[str, Any]:
    table = quote_identifier(table_names[rule["asset"]])
    column = quote_identifier(rule["column"])
    expectation = rule["expectation"]
    expectation_type = expectation["type"]
    evaluate_filter = (
        "TRUE"
        if rule["empty_policy"] == "evaluate"
        else f"(s.{column} IS NULL OR s.{column} <> '')"
    )
    rows_total = connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    rows_evaluated = connection.execute(
        f"SELECT count(*) FROM {table} s WHERE {evaluate_filter}"
    ).fetchone()[0]
    parameters: list[Any] = []

    if expectation_type == "allowed_values":
        placeholders = ", ".join("?" for _ in expectation["values"])
        violation = f"(s.{column} IS NULL OR s.{column} NOT IN ({placeholders}))"
        parameters = list(expectation["values"])
    elif expectation_type == "regex_format":
        violation = f"(s.{column} IS NULL OR NOT regexp_full_match(s.{column}, ?))"
        parameters = [expectation["pattern"]]
    elif expectation_type == "reference_exists":
        reference = expectation["reference"]
        target_table = quote_identifier(table_names[reference["asset"]])
        target_column = quote_identifier(reference["column"])
        violation = (
            f"NOT EXISTS (SELECT 1 FROM {target_table} t "
            f"WHERE t.{target_column} = s.{column})"
        )
    elif expectation_type == "unique":
        violations = connection.execute(
            f"""
            SELECT coalesce(sum(occurrences), 0)
            FROM (
                SELECT count(*) AS occurrences
                FROM {table} s
                WHERE {evaluate_filter}
                GROUP BY s.{column}
                HAVING count(*) > 1
            ) duplicates
            """
        ).fetchone()[0]
        samples = [
            sample_text(row[0])
            for row in connection.execute(
                f"""
                SELECT s.{column}
                FROM {table} s
                WHERE {evaluate_filter}
                GROUP BY s.{column}
                HAVING count(*) > 1
                ORDER BY s.{column} NULLS FIRST
                LIMIT {SAMPLE_SIZE}
                """
            ).fetchall()
        ]
        return build_rule_result(rule, rows_total, rows_evaluated, violations, samples)
    else:
        raise ValueError(f"Unsupported expectation type: {expectation_type}")

    violations = connection.execute(
        f"SELECT count(*) FROM {table} s WHERE {evaluate_filter} AND {violation}",
        parameters,
    ).fetchone()[0]
    samples = [
        sample_text(row[0])
        for row in connection.execute(
            f"""
            SELECT DISTINCT s.{column}
            FROM {table} s
            WHERE {evaluate_filter} AND {violation}
            ORDER BY s.{column} NULLS FIRST
            LIMIT {SAMPLE_SIZE}
            """,
            parameters,
        ).fetchall()
    ]
    return build_rule_result(rule, rows_total, rows_evaluated, violations, samples)


def build_rule_result(
    rule: dict[str, Any],
    rows_total: int,
    rows_evaluated: int,
    violations: int,
    samples: list[str],
) -> dict[str, Any]:
    return {
        "rule_id": rule["id"],
        "asset": rule["asset"],
        "column": rule["column"],
        "dimension": rule["dimension"],
        "expectation_type": rule["expectation"]["type"],
        "empty_policy": rule["empty_policy"],
        "status": "PASSED" if violations == 0 else "FAILED",
        "rows_total": rows_total,
        "rows_evaluated": rows_evaluated,
        "violations": violations,
        "compliance_ratio": (
            (rows_evaluated - violations) / rows_evaluated
            if rows_evaluated > 0
            else None
        ),
        "sample_violations": samples,
    }


def write_execution_record(record: dict[str, Any], results_path: Path) -> Path:
    results_path.mkdir(parents=True, exist_ok=True)
    record_path = results_path / f"{record['run_id']}.json"
    with record_path.open("x", encoding="utf-8", newline="\n") as output_file:
        json.dump(record, output_file, indent=2, ensure_ascii=False)
        output_file.write("\n")
    return record_path


def run_dq(config: DQConfig) -> dict[str, Any]:
    timer_started = time.perf_counter()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + f"-{uuid.uuid4().hex[:8]}"
    record: dict[str, Any] = {
        "run_id": run_id,
        "started_at": utc_now(),
        "completed_at": None,
        "elapsed_seconds": None,
        "status": "FAILED",
        "data_zone": config.data_zone,
        "data_path": str(config.data_path.resolve()),
        "rules_total": 0,
        "rules_passed": 0,
        "rules_failed": 0,
        "execution_errors": [],
        "rule_results": [],
    }
    connection: duckdb.DuckDBPyConnection | None = None
    try:
        if config.data_zone not in DATA_ZONES:
            raise ValueError(
                f"Unsupported data_zone {config.data_zone}; expected one of: "
                + ", ".join(sorted(DATA_ZONES))
            )
        manifest_entries = load_manifest(config.manifest_path)
        manifest_files = {str(entry["name"]) for entry in manifest_entries}
        delimiter, quote = load_physical_format(config.manifest_path)
        catalog = load_catalog(config.catalog_path)
        relationships = load_relationships(config.relationships_path)
        rules = load_rules(config.rules_path)
        record["rules_total"] = len(rules)
        metadata_errors = validate_rule_metadata(
            rules, catalog, relationships, manifest_files
        )
        if metadata_errors:
            record["execution_errors"] = metadata_errors
            raise ValueError(f"Rule metadata validation failed with {len(metadata_errors)} error(s)")

        connection = duckdb.connect(database=":memory:")
        table_names = load_raw_tables(
            connection,
            catalog,
            rules,
            config.data_path,
            delimiter,
            quote,
        )
        for rule in rules:
            LOGGER.info("Executing %s", rule["id"])
            record["rule_results"].append(execute_rule(connection, rule, table_names))
        record["rules_passed"] = sum(
            result["status"] == "PASSED" for result in record["rule_results"]
        )
        record["rules_failed"] = sum(
            result["status"] == "FAILED" for result in record["rule_results"]
        )
        record["status"] = "SUCCESS"
    except Exception as exc:
        if not record["execution_errors"]:
            record["execution_errors"] = [
                {"rule_id": "<execution>", "message": f"{type(exc).__name__}: {exc}"}
            ]
        LOGGER.error("DQ execution failed: %s", exc)
    finally:
        if connection is not None:
            connection.close()
        record["completed_at"] = utc_now()
        record["elapsed_seconds"] = round(time.perf_counter() - timer_started, 6)
        try:
            execution_path = write_execution_record(record, config.results_path)
            record["execution_record"] = str(execution_path.resolve())
        except Exception as exc:
            LOGGER.error("Could not write DQ execution record: %s", exc)
            record["execution_record"] = "UNAVAILABLE"
            record["status"] = "FAILED"
    return record
