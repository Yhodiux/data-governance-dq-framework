"""Rebuild a queryable DuckDB projection from immutable DQ JSON records."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

LOGGER = logging.getLogger(__name__)

RUN_REQUIRED_FIELDS = (
    "run_id",
    "started_at",
    "completed_at",
    "elapsed_seconds",
    "status",
    "rules_total",
    "rules_passed",
    "rules_failed",
    "execution_errors",
    "rule_results",
)
RULE_REQUIRED_FIELDS = (
    "rule_id",
    "asset",
    "column",
    "dimension",
    "expectation_type",
    "empty_policy",
    "status",
    "rows_total",
    "rows_evaluated",
    "violations",
    "compliance_ratio",
    "sample_violations",
)


@dataclass(frozen=True)
class ObservabilityConfig:
    source_path: Path
    database_path: Path
    runs_path: Path


class SourceRecordError(ValueError):
    def __init__(self, source_record: str, message: str) -> None:
        super().__init__(message)
        self.source_record = source_record
        self.message = message


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def require_nonempty_text(record: dict[str, Any], field: str, source: str) -> None:
    if not isinstance(record.get(field), str) or not record[field]:
        raise SourceRecordError(source, f"{field} must be non-empty text")


def require_timestamp(record: dict[str, Any], field: str, source: str) -> None:
    require_nonempty_text(record, field, source)
    try:
        datetime.fromisoformat(record[field].replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceRecordError(source, f"{field} must be an ISO-8601 timestamp") from exc


def validate_rule_result(rule: Any, source: str, index: int) -> dict[str, Any]:
    if not isinstance(rule, dict):
        raise SourceRecordError(source, f"rule_results[{index}] must be an object")
    missing = [field for field in RULE_REQUIRED_FIELDS if field not in rule]
    if missing:
        raise SourceRecordError(
            source,
            f"rule_results[{index}] is missing required field(s): {', '.join(missing)}",
        )
    for field in (
        "rule_id",
        "asset",
        "column",
        "dimension",
        "expectation_type",
        "empty_policy",
        "status",
    ):
        require_nonempty_text(rule, field, source)
    if rule["status"] not in {"PASSED", "FAILED"}:
        raise SourceRecordError(source, f"rule_results[{index}].status is unsupported")
    for field in ("rows_total", "rows_evaluated", "violations"):
        if not is_nonnegative_int(rule[field]):
            raise SourceRecordError(
                source, f"rule_results[{index}].{field} must be a non-negative integer"
            )
    ratio = rule["compliance_ratio"]
    if ratio is not None and (
        not isinstance(ratio, (int, float))
        or isinstance(ratio, bool)
        or not 0 <= ratio <= 1
    ):
        raise SourceRecordError(
            source, f"rule_results[{index}].compliance_ratio must be null or between 0 and 1"
        )
    samples = rule["sample_violations"]
    if not isinstance(samples, list) or any(not isinstance(value, str) for value in samples):
        raise SourceRecordError(
            source, f"rule_results[{index}].sample_violations must be a text list"
        )
    return rule


def validate_run_record(document: Any, source: str) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise SourceRecordError(source, "Execution record must be a JSON object")
    missing = [field for field in RUN_REQUIRED_FIELDS if field not in document]
    if missing:
        raise SourceRecordError(
            source, f"Missing required run field(s): {', '.join(missing)}"
        )
    for field in ("run_id", "status"):
        require_nonempty_text(document, field, source)
    if document["status"] not in {"SUCCESS", "FAILED"}:
        raise SourceRecordError(source, "status must be SUCCESS or FAILED")
    for field in ("started_at", "completed_at"):
        require_timestamp(document, field, source)
    elapsed = document["elapsed_seconds"]
    if (
        not isinstance(elapsed, (int, float))
        or isinstance(elapsed, bool)
        or elapsed < 0
    ):
        raise SourceRecordError(source, "elapsed_seconds must be a non-negative number")
    for field in ("rules_total", "rules_passed", "rules_failed"):
        if not is_nonnegative_int(document[field]):
            raise SourceRecordError(source, f"{field} must be a non-negative integer")
    if not isinstance(document["execution_errors"], list):
        raise SourceRecordError(source, "execution_errors must be a list")
    if not isinstance(document["rule_results"], list):
        raise SourceRecordError(source, "rule_results must be a list")

    has_data_zone = "data_zone" in document
    has_data_path = "data_path" in document
    if has_data_zone != has_data_path:
        raise SourceRecordError(
            source, "data_zone and data_path must either both be present or both be absent"
        )
    if has_data_zone:
        require_nonempty_text(document, "data_zone", source)
        require_nonempty_text(document, "data_path", source)
        if document["data_zone"] not in {"raw", "trusted"}:
            raise SourceRecordError(source, "data_zone must be raw or trusted")

    validated_results = [
        validate_rule_result(rule, source, index)
        for index, rule in enumerate(document["rule_results"])
    ]
    rule_ids = [rule["rule_id"] for rule in validated_results]
    if len(rule_ids) != len(set(rule_ids)):
        raise SourceRecordError(source, "rule_id must be unique within a DQ run")

    if document["status"] == "SUCCESS":
        if document["rules_total"] != len(validated_results):
            raise SourceRecordError(
                source,
                "Successful run rules_total does not match rule_results count",
            )
        passed = sum(rule["status"] == "PASSED" for rule in validated_results)
        failed = sum(rule["status"] == "FAILED" for rule in validated_results)
        if document["rules_passed"] != passed or document["rules_failed"] != failed:
            raise SourceRecordError(
                source,
                "Successful run passed/failed counts do not match rule_results",
            )
        if document["rules_passed"] + document["rules_failed"] != document["rules_total"]:
            raise SourceRecordError(
                source,
                "Successful run passed/failed counts do not add up to rules_total",
            )
    return document


def discover_and_validate(source_path: Path) -> list[tuple[str, dict[str, Any]]]:
    source_files = sorted(source_path.glob("*.json"), key=lambda path: path.name)
    records: list[tuple[str, dict[str, Any]]] = []
    run_sources: dict[str, str] = {}
    for path in source_files:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SourceRecordError(path.name, f"Cannot read valid JSON: {exc}") from exc
        record = validate_run_record(document, path.name)
        run_id = record["run_id"]
        if run_id in run_sources:
            raise SourceRecordError(
                path.name,
                f"Duplicate run_id {run_id}; first seen in {run_sources[run_id]}",
            )
        run_sources[run_id] = path.name
        records.append((path.name, record))
    return records


def create_schema(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        """
        CREATE TABLE dq_runs (
            run_id VARCHAR PRIMARY KEY,
            started_at TIMESTAMP NOT NULL,
            completed_at TIMESTAMP NOT NULL,
            elapsed_seconds DOUBLE NOT NULL,
            status VARCHAR NOT NULL,
            rules_total BIGINT NOT NULL,
            rules_passed BIGINT NOT NULL,
            rules_failed BIGINT NOT NULL,
            execution_error_count BIGINT NOT NULL,
            source_record VARCHAR NOT NULL,
            data_zone VARCHAR,
            data_path VARCHAR
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE dq_rule_results (
            run_id VARCHAR NOT NULL,
            rule_id VARCHAR NOT NULL,
            asset VARCHAR NOT NULL,
            column_name VARCHAR NOT NULL,
            dimension VARCHAR NOT NULL,
            expectation_type VARCHAR NOT NULL,
            empty_policy VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            rows_total BIGINT NOT NULL,
            rows_evaluated BIGINT NOT NULL,
            violations BIGINT NOT NULL,
            compliance_ratio DOUBLE,
            sample_violations JSON NOT NULL,
            source_record VARCHAR NOT NULL,
            PRIMARY KEY (run_id, rule_id)
        )
        """
    )


def populate_database(
    database_path: Path, records: list[tuple[str, dict[str, Any]]]
) -> tuple[int, int]:
    connection = duckdb.connect(str(database_path))
    try:
        create_schema(connection)
        rule_result_count = 0
        for source_record, record in records:
            connection.execute(
                """
                INSERT INTO dq_runs VALUES (
                    ?,
                    CAST(? AS TIMESTAMPTZ) AT TIME ZONE 'UTC',
                    CAST(? AS TIMESTAMPTZ) AT TIME ZONE 'UTC',
                    ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    record["run_id"],
                    record["started_at"],
                    record["completed_at"],
                    record["elapsed_seconds"],
                    record["status"],
                    record["rules_total"],
                    record["rules_passed"],
                    record["rules_failed"],
                    len(record["execution_errors"]),
                    source_record,
                    record.get("data_zone"),
                    record.get("data_path"),
                ],
            )
            for rule in record["rule_results"]:
                connection.execute(
                    """
                    INSERT INTO dq_rule_results VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    [
                        record["run_id"],
                        rule["rule_id"],
                        rule["asset"],
                        rule["column"],
                        rule["dimension"],
                        rule["expectation_type"],
                        rule["empty_policy"],
                        rule["status"],
                        rule["rows_total"],
                        rule["rows_evaluated"],
                        rule["violations"],
                        rule["compliance_ratio"],
                        json.dumps(rule["sample_violations"], ensure_ascii=False),
                        source_record,
                    ],
                )
                rule_result_count += 1
        connection.execute("CHECKPOINT")
    finally:
        connection.close()
    return len(records), rule_result_count


def rebuild_atomically(
    database_path: Path, records: list[tuple[str, dict[str, Any]]]
) -> tuple[int, int]:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    staging_path = database_path.parent / (
        f".{database_path.name}.staging-{uuid.uuid4().hex}"
    )
    try:
        counts = populate_database(staging_path, records)
        os.replace(staging_path, database_path)
        return counts
    finally:
        if staging_path.exists():
            staging_path.unlink()


def write_build_record(record: dict[str, Any], runs_path: Path) -> Path:
    runs_path.mkdir(parents=True, exist_ok=True)
    path = runs_path / f"{record['run_id']}.json"
    with path.open("x", encoding="utf-8", newline="\n") as output_file:
        json.dump(record, output_file, indent=2, ensure_ascii=False)
        output_file.write("\n")
    return path


def run_observability_build(config: ObservabilityConfig) -> dict[str, Any]:
    timer_started = time.perf_counter()
    run_id = "obs-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + f"-{uuid.uuid4().hex[:8]}"
    source_files = sorted(config.source_path.glob("*.json"), key=lambda path: path.name)
    record: dict[str, Any] = {
        "run_id": run_id,
        "started_at": utc_now(),
        "completed_at": None,
        "elapsed_seconds": None,
        "status": "FAILED",
        "source_records_discovered": len(source_files),
        "dq_runs_loaded": 0,
        "dq_rule_results_loaded": 0,
        "errors": [],
        "database_path": str(config.database_path.resolve()),
    }
    try:
        records = discover_and_validate(config.source_path)
        runs_loaded, rule_results_loaded = rebuild_atomically(
            config.database_path, records
        )
        record["dq_runs_loaded"] = runs_loaded
        record["dq_rule_results_loaded"] = rule_results_loaded
        record["status"] = "SUCCESS"
    except SourceRecordError as exc:
        record["errors"].append(
            {"source_record": exc.source_record, "message": exc.message}
        )
        LOGGER.error("Observability build failed for %s: %s", exc.source_record, exc.message)
    except Exception as exc:
        record["errors"].append(
            {"source_record": "<build>", "message": f"{type(exc).__name__}: {exc}"}
        )
        LOGGER.error("Observability build failed: %s", exc)
    finally:
        record["completed_at"] = utc_now()
        record["elapsed_seconds"] = round(time.perf_counter() - timer_started, 6)
        try:
            build_record_path = write_build_record(record, config.runs_path)
            record["execution_record"] = str(build_record_path.resolve())
        except Exception as exc:
            LOGGER.error("Could not write observability build record: %s", exc)
            record["execution_record"] = "UNAVAILABLE"
            record["status"] = "FAILED"
    return record
