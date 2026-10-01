"""Project only the facts present in authoritative execution JSON records."""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

METRICS = {
    "ingestion": ("files_expected", "files_processed"),
    "dq": ("rules_total", "rules_passed", "rules_failed"),
    "standardization": ("policies_total", "policies_applied", "assets_total", "assets_copied", "assets_transformed"),
    "replay": (),
}
DETAILS = {
    "ingestion": ("files", "INGESTION_FILE", "file_name"),
    "dq": ("rule_results", "DQ_RULE", "rule_id"),
    "standardization": ("policy_results", "STANDARDIZATION_POLICY", "policy_id"),
    "replay": ("assets", "REPLAY_ASSET", "asset"),
}
DETAIL_COLUMNS = (
    "detail_id", "run_id", "detail_type", "semantic_id", "asset", "column_name", "status",
    "rows_total", "rows_evaluated", "rows_changed", "violations", "compliance_ratio",
    "input_rows", "output_rows", "attributes",
)


@dataclass(frozen=True)
class TraceabilityConfig:
    root_path: Path

    @property
    def database_path(self) -> Path:
        return self.root_path / "data/results/traceability/execution_traceability.duckdb"


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def unique_mapping(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def text(value, label, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be nonempty text")
    return value


def number(value, label, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    if integer and (not isinstance(value, int) or not 0 <= value <= 9223372036854775807):
        raise ValueError(f"{label} must be a nonnegative BIGINT")
    return value


def timestamp(value, label):
    text(value, label)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone required")
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError as exc:
        raise ValueError(f"Invalid {label}: {value}") from exc


def cutoff(value):
    if value is None:
        return None
    text(value, "cutoff")
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError("ISO YYYY-MM-DD required")
        return parsed
    except ValueError as exc:
        raise ValueError(f"Invalid cutoff: {value}") from exc


def detail_row(process, run_id, item):
    if not isinstance(item, dict):
        raise ValueError("Detail must be an object")
    _, kind, semantic_key = DETAILS[process]
    semantic = text(item.get(semantic_key), semantic_key)
    detail_id = "detail:" + hashlib.sha256(canonical([process, run_id, kind, semantic]).encode("utf-8")).hexdigest()
    row = dict.fromkeys(DETAIL_COLUMNS)
    row.update(detail_id=detail_id, run_id=run_id, detail_type=kind, semantic_id=semantic)
    attributes = {}
    if process == "ingestion":
        row["status"] = text(item.get("validation_status"), "validation_status")
        attribute_keys = ("expected_sha256", "actual_sha256", "expected_record_count", "actual_record_count")
    else:
        row["asset"] = text(item.get("asset"), "asset")
        if process in {"dq", "standardization"}:
            row["column_name"] = text(item.get("column"), "column")
        if process == "dq":
            row["status"] = text(item.get("status"), "rule status")
            attribute_keys = ("dimension", "expectation_type", "empty_policy", "sample_violations")
        elif process == "standardization":
            attribute_keys = ("transformation_type", "rows_unchanged", "sample_changes")
        else:
            attribute_keys = ("strategy",)
    numeric_keys = {
        "ingestion": (), "dq": ("rows_total", "rows_evaluated", "violations", "compliance_ratio"),
        "standardization": ("rows_total", "rows_evaluated", "rows_changed"),
        "replay": ("input_rows", "output_rows"),
    }[process]
    for key in numeric_keys:
        if key not in item:
            raise ValueError(f"Detail missing {key}")
        row[key] = None if key == "compliance_ratio" and item[key] is None else number(item[key], key, integer=key != "compliance_ratio")
    for key in attribute_keys:
        if key in item:
            value = item[key]
            if key in {"expected_record_count", "actual_record_count", "rows_unchanged"}:
                number(value, key, integer=True)
            elif key in {"sample_changes", "sample_violations"}:
                if not isinstance(value, list):
                    raise ValueError(f"{key} must be an array")
                for sample in value:
                    if key == "sample_changes":
                        if not isinstance(sample, dict) or not all(isinstance(sample.get(k), str) for k in ("before", "after")):
                            raise ValueError("Invalid sample change")
                    elif not isinstance(sample, str):
                        raise ValueError("Invalid sample violation")
            else:
                text(value, key)
            attributes[key] = value
    row["attributes"] = canonical(attributes)
    return tuple(row[key] for key in DETAIL_COLUMNS)


def load_projection(config: TraceabilityConfig):
    root = config.root_path.resolve()
    runs, metrics, details = {}, {}, {}
    for process in sorted(METRICS):
        for path in sorted((root / "data/results" / process).glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_mapping)
                if not isinstance(record, dict):
                    raise ValueError("Record must be an object")
                if "process_type" in record and record["process_type"] != process:
                    raise ValueError("Unknown or conflicting process_type")
                run_id = text(record.get("run_id"), "run_id")
                if path.name != run_id + ".json":
                    raise ValueError("Filename does not match run_id")
                if run_id in runs:
                    raise ValueError(f"Duplicate run_id: {run_id}")
                status = text(record.get("status"), "status")
                if status not in {"SUCCESS", "FAILED"}:
                    raise ValueError("Unsupported run status")
                start = timestamp(record.get("started_at"), "started_at")
                end = timestamp(record.get("completed_at"), "completed_at")
                if end < start:
                    raise ValueError("completed_at precedes started_at")
                elapsed = record.get("elapsed_seconds")
                if elapsed is not None:
                    number(elapsed, "elapsed_seconds")
                    if elapsed < 0:
                        raise ValueError("Negative elapsed_seconds")
                zone = record.get("data_zone") if process == "dq" else record.get("source_zone") if process == "replay" else None
                zone = text(zone, "data_zone", nullable=True)
                if zone is not None and zone not in {"raw", "trusted", "snapshot"}:
                    raise ValueError("Unsupported data_zone")
                date_value = cutoff(record.get("snapshot_cutoff") if process == "dq" else record.get("cutoff") if process == "replay" else None)
                source_key = {"ingestion": "source_path", "dq": "data_path", "replay": "source_path"}.get(process)
                output_key = {"ingestion": "raw_path", "standardization": "trusted_path", "replay": "snapshot_path"}.get(process)
                source = text(record.get(source_key), "source_path", nullable=True) if source_key else None
                output = text(record.get(output_key), "output_path", nullable=True) if output_key else None
                runs[run_id] = (run_id, process, status, start, end, elapsed, zone, date_value, source, output, path.relative_to(root).as_posix())
                for name in METRICS[process]:
                    if name in record:
                        key = (run_id, name)
                        if key in metrics:
                            raise ValueError("Duplicate metric")
                        metrics[key] = (run_id, name, number(record[name], name, integer=True))
                array_key, _, _ = DETAILS[process]
                if not isinstance(record.get(array_key), list):
                    raise ValueError(f"{array_key} must be an array")
                for item in record[array_key]:
                    row = detail_row(process, run_id, item)
                    if row[0] in details:
                        raise ValueError("Duplicate detail ID")
                    details[row[0]] = row
            except Exception as exc:
                raise ValueError(f"{path.relative_to(root).as_posix()}: {exc}") from exc
    return tuple([mapping[key] for key in sorted(mapping)] for mapping in (runs, metrics, details))


def publish_projection(config, rows):
    database = config.database_path.resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    staging = database.with_name("." + database.name + ".staging")
    connection, published = None, False
    try:
        connection = duckdb.connect(str(staging))
        connection.execute("""CREATE TABLE execution_runs (
            run_id VARCHAR PRIMARY KEY, process_type VARCHAR NOT NULL,
            status VARCHAR NOT NULL, started_at TIMESTAMP NOT NULL, completed_at TIMESTAMP NOT NULL,
            elapsed_seconds DOUBLE, data_zone VARCHAR, snapshot_cutoff DATE,
            source_path VARCHAR, output_path VARCHAR, execution_record_path VARCHAR NOT NULL,
            CHECK (process_type IN ('ingestion','dq','standardization','replay')),
            CHECK (completed_at >= started_at))""")
        connection.execute("""CREATE TABLE execution_metrics (
            run_id VARCHAR NOT NULL REFERENCES execution_runs(run_id),
            metric_name VARCHAR NOT NULL, metric_value DOUBLE NOT NULL,
            PRIMARY KEY(run_id,metric_name))""")
        connection.execute("""CREATE TABLE execution_details (
            detail_id VARCHAR PRIMARY KEY, run_id VARCHAR NOT NULL REFERENCES execution_runs(run_id),
            detail_type VARCHAR NOT NULL, semantic_id VARCHAR, asset VARCHAR, column_name VARCHAR,
            status VARCHAR, rows_total BIGINT, rows_evaluated BIGINT, rows_changed BIGINT,
            violations BIGINT, compliance_ratio DOUBLE, input_rows BIGINT, output_rows BIGINT,
            attributes JSON NOT NULL,
            CHECK (detail_type IN ('INGESTION_FILE','DQ_RULE','STANDARDIZATION_POLICY','REPLAY_ASSET')))""")
        for table, width, values in zip(("execution_runs", "execution_metrics", "execution_details"), (11, 3, 15), rows):
            if values:
                connection.executemany(f"INSERT INTO {table} VALUES ({','.join('?' for _ in range(width))})", values)
            if connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] != len(values):
                raise ValueError("Staging count mismatch")
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


def run_traceability(config: TraceabilityConfig) -> dict:
    result = dict(status="FAILED", runs=0, metrics=0, details=0,
                  database_path=str(config.database_path.resolve()), errors=[])
    try:
        rows = load_projection(config)
        result.update(zip(("runs", "metrics", "details"), map(len, rows)))
        publish_projection(config, rows)
        result["status"] = "SUCCESS"
    except Exception as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    return result
