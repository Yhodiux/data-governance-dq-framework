"""Read only observability; summarize evaluations without a global score."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import duckdb

SOURCE_RUN_COLUMNS = ("run_id", "started_at", "data_zone", "snapshot_cutoff")
SOURCE_RESULT_COLUMNS = (
    "run_id", "rule_id", "asset", "column_name", "dimension", "expectation_type",
    "empty_policy", "status", "rows_total", "rows_evaluated", "violations", "compliance_ratio",
)
LEVELS = (
    ("RUN", "run_id,data_zone,snapshot_cutoff", "NULL", "NULL", False),
    ("RUN_ASSET", "run_id,data_zone,snapshot_cutoff,asset", "asset", "NULL", False),
    ("RUN_DIMENSION", "run_id,data_zone,snapshot_cutoff,dimension", "NULL", "dimension", False),
    ("SNAPSHOT", "data_zone,snapshot_cutoff", "NULL", "NULL", True),
    ("SNAPSHOT_ASSET", "data_zone,snapshot_cutoff,asset", "asset", "NULL", True),
    ("SNAPSHOT_DIMENSION", "data_zone,snapshot_cutoff,dimension", "NULL", "dimension", True),
)


@dataclass(frozen=True)
class MetricsConfig:
    root_path: Path

    @property
    def source_path(self) -> Path:
        return self.root_path / "data/results/observability/dq_history.duckdb"

    @property
    def database_path(self) -> Path:
        return self.root_path / "data/results/metrics/dq_metrics.duckdb"


def required_text(value, label):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be nonempty text")


def load_evaluations(config: MetricsConfig) -> list[tuple]:
    if not config.source_path.is_file():
        raise ValueError("Observability database is missing")
    with duckdb.connect(str(config.source_path.resolve()), read_only=True) as source:
        run_rows = source.execute(f"SELECT {','.join(SOURCE_RUN_COLUMNS)} FROM dq_runs ORDER BY run_id").fetchall()
        results = source.execute(f"SELECT {','.join(SOURCE_RESULT_COLUMNS)} FROM dq_rule_results ORDER BY run_id,rule_id").fetchall()
    runs = {}
    for run_id, started, zone, cutoff in run_rows:
        required_text(run_id, "run_id")
        if run_id in runs:
            raise ValueError("Duplicate run_id")
        if not isinstance(started, datetime):
            raise ValueError("Invalid started_at")
        if zone is not None:
            required_text(zone, "data_zone")
        if cutoff is not None and not isinstance(cutoff, date):
            raise ValueError("Invalid snapshot_cutoff")
        runs[run_id] = (started, zone, cutoff)
    evaluations, seen = [], set()
    for result in results:
        run_id, rule_id, asset, column, dimension, expectation, empty, status, total, evaluated, violations, compliance = result
        for label, value in zip(SOURCE_RESULT_COLUMNS[:8], result[:8]):
            required_text(value, label)
        if run_id not in runs:
            raise ValueError(f"Unknown run: {run_id}")
        if (run_id, rule_id) in seen:
            raise ValueError("Duplicate (run_id, rule_id)")
        seen.add((run_id, rule_id))
        if status not in {"PASSED", "FAILED"}:
            raise ValueError("Unsupported evaluation status")
        for label, value in (("rows_total", total), ("rows_evaluated", evaluated), ("violations", violations)):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 9223372036854775807:
                raise ValueError(f"Invalid {label}")
        if evaluated > total or violations > evaluated:
            raise ValueError("Invalid T/E/V ordering")
        if compliance is not None:
            if isinstance(compliance, bool) or not isinstance(compliance, (int, float)) or not math.isfinite(compliance) or not 0 <= compliance <= 1:
                raise ValueError("Invalid compliance_ratio")
        if evaluated:
            expected = (evaluated - violations) / evaluated
            if compliance is None or not math.isclose(compliance, expected, rel_tol=0, abs_tol=1e-12):
                raise ValueError("Inconsistent compliance_ratio")
        elif compliance is not None:
            raise ValueError("compliance_ratio must be NULL for zero denominator")
        started, zone, cutoff = runs[run_id]
        evaluations.append((run_id, rule_id, started, zone, cutoff, asset, column, dimension,
                            expectation, empty, status, total, evaluated, total - evaluated,
                            evaluated / total if total else None, violations, evaluated - violations,
                            violations / evaluated if evaluated else None, compliance))
    return evaluations


def publish_metrics(config: MetricsConfig, evaluations: list[tuple]) -> int:
    database = config.database_path.resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    staging = database.with_name("." + database.name + ".staging")
    connection, published = None, False
    try:
        connection = duckdb.connect(str(staging))
        connection.execute("""CREATE TABLE dq_evaluation_metrics (
            run_id VARCHAR NOT NULL, rule_id VARCHAR NOT NULL, started_at TIMESTAMP NOT NULL,
            data_zone VARCHAR, snapshot_cutoff DATE, asset VARCHAR NOT NULL,
            column_name VARCHAR NOT NULL, dimension VARCHAR NOT NULL,
            expectation_type VARCHAR NOT NULL, empty_policy VARCHAR NOT NULL, status VARCHAR NOT NULL,
            rows_total BIGINT NOT NULL, rows_evaluated BIGINT NOT NULL, rows_not_evaluated BIGINT NOT NULL,
            evaluation_ratio DOUBLE, violations BIGINT NOT NULL, conforming_evaluations BIGINT NOT NULL,
            violation_ratio DOUBLE, compliance_ratio DOUBLE,
            PRIMARY KEY(run_id,rule_id), CHECK(status IN ('PASSED','FAILED')),
            CHECK(rows_total >= rows_evaluated AND rows_evaluated >= violations AND violations >= 0))""")
        if evaluations:
            connection.executemany("INSERT INTO dq_evaluation_metrics VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", evaluations)
        connection.execute("""CREATE TABLE dq_aggregate_metrics (
            aggregation_level VARCHAR NOT NULL, run_id VARCHAR, data_zone VARCHAR, snapshot_cutoff DATE,
            asset VARCHAR, dimension VARCHAR, evaluations BIGINT NOT NULL, distinct_rules BIGINT NOT NULL,
            passed_evaluations BIGINT NOT NULL, failed_evaluations BIGINT NOT NULL, pass_ratio DOUBLE,
            row_rule_evaluations HUGEINT NOT NULL, row_rule_violations HUGEINT NOT NULL,
            row_rule_conforming HUGEINT NOT NULL, weighted_violation_ratio DOUBLE, weighted_compliance_ratio DOUBLE,
            CHECK(aggregation_level IN ('RUN','RUN_ASSET','RUN_DIMENSION','SNAPSHOT','SNAPSHOT_ASSET','SNAPSHOT_DIMENSION')))""")
        for level, group, asset, dimension, snapshot in LEVELS:
            run = "NULL" if snapshot else "run_id"
            where = "WHERE data_zone='snapshot' AND snapshot_cutoff IS NOT NULL" if snapshot else ""
            connection.execute(f"""INSERT INTO dq_aggregate_metrics
                SELECT '{level}', {run}, data_zone, snapshot_cutoff, {asset}, {dimension},
                    count(*), count(DISTINCT rule_id), count(*) FILTER(WHERE status='PASSED'),
                    count(*) FILTER(WHERE status='FAILED'),
                    count(*) FILTER(WHERE status='PASSED')::DOUBLE / nullif(count(*),0),
                    sum(rows_evaluated), sum(violations), sum(conforming_evaluations),
                    sum(violations)::DOUBLE / nullif(sum(rows_evaluated),0),
                    sum(conforming_evaluations)::DOUBLE / nullif(sum(rows_evaluated),0)
                FROM dq_evaluation_metrics {where} GROUP BY {group} ORDER BY {group}""")
        if connection.execute("SELECT count(*) FROM dq_evaluation_metrics").fetchone()[0] != len(evaluations):
            raise ValueError("Evaluation count mismatch")
        aggregates = connection.execute("SELECT count(*) FROM dq_aggregate_metrics").fetchone()[0]
        connection.execute("CHECKPOINT")
        connection.close()
        connection = None
        os.replace(staging, database)
        published = True
        return aggregates
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


def run_metrics(config: MetricsConfig) -> dict:
    result = dict(status="FAILED", evaluations=0, aggregates=0,
                  database_path=str(config.database_path.resolve()), errors=[])
    try:
        evaluations = load_evaluations(config)
        result["evaluations"] = len(evaluations)
        result["aggregates"] = publish_metrics(config, evaluations)
        result["status"] = "SUCCESS"
    except Exception as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    return result
