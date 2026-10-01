"""Physical, non-semantic profiling of manifest-declared RAW files."""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import yaml

from src.ingestion.core import load_manifest

LOGGER = logging.getLogger(__name__)
SAMPLE_SIZE = 5


@dataclass(frozen=True)
class ProfilingConfig:
    """Filesystem locations used by one profiling run."""

    raw_path: Path
    manifest_path: Path
    results_path: Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def quote_identifier(identifier: str) -> str:
    """Quote a DuckDB identifier without changing its text."""

    return '"' + identifier.replace('"', '""') + '"'


def load_physical_format(manifest_path: Path) -> tuple[str, str]:
    """Load parser settings already established in the dataset manifest."""

    with manifest_path.open("r", encoding="utf-8") as manifest_file:
        manifest = yaml.safe_load(manifest_file)
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
        raise ValueError("Local profiling requires the manifest header setting to be true")
    return delimiter, quote


def profile_column(
    connection: duckdb.DuckDBPyConnection,
    column_name: str,
    row_count: int,
) -> dict[str, Any]:
    """Calculate metrics over one VARCHAR column in the temporary RAW table."""

    column = quote_identifier(column_name)
    metrics = connection.execute(
        f"""
        SELECT
            count(*) FILTER (WHERE {column} = '') AS empty_count,
            count(*) FILTER (WHERE {column} IS NULL) AS parser_null_count,
            count(*) FILTER (WHERE {column} IS NOT NULL AND {column} <> '')
                AS non_empty_count,
            count(DISTINCT {column}) FILTER (
                WHERE {column} IS NOT NULL AND {column} <> ''
            ) AS distinct_count,
            min({column}) FILTER (WHERE {column} IS NOT NULL AND {column} <> '')
                AS min_observed,
            max({column}) FILTER (WHERE {column} IS NOT NULL AND {column} <> '')
                AS max_observed
        FROM profiled_raw
        """
    ).fetchone()
    if metrics is None:
        raise RuntimeError(f"DuckDB returned no metrics for column {column_name}")

    empty_count, parser_null_count, non_empty_count, distinct_count, minimum, maximum = (
        metrics
    )
    samples = [
        row[0]
        for row in connection.execute(
            f"""
            SELECT DISTINCT {column}
            FROM profiled_raw
            WHERE {column} IS NOT NULL AND {column} <> ''
            ORDER BY {column}
            LIMIT {SAMPLE_SIZE}
            """
        ).fetchall()
    ]

    return {
        "column_name": column_name,
        "total_count": row_count,
        "empty_count": empty_count,
        "parser_null_count": parser_null_count,
        "non_empty_count": non_empty_count,
        "distinct_count": distinct_count,
        "uniqueness_ratio": (
            distinct_count / non_empty_count if non_empty_count > 0 else None
        ),
        "min_observed": minimum,
        "max_observed": maximum,
        "sample_values": samples,
    }


def profile_file(
    connection: duckdb.DuckDBPyConnection,
    file_path: Path,
    delimiter: str,
    quote: str,
) -> dict[str, Any]:
    """Load one RAW file as VARCHAR columns and calculate physical metrics."""

    connection.execute("DROP TABLE IF EXISTS profiled_raw")
    connection.execute(
        """
        CREATE TEMP TABLE profiled_raw AS
        SELECT *
        FROM read_csv(
            ?,
            header = true,
            delim = ?,
            quote = ?,
            all_varchar = true,
            nullstr = chr(0)
        )
        """,
        [str(file_path), delimiter, quote],
    )
    schema = connection.execute("DESCRIBE profiled_raw").fetchall()
    column_names = [row[0] for row in schema]
    row_count = connection.execute("SELECT count(*) FROM profiled_raw").fetchone()[0]

    return {
        "file_name": file_path.name,
        "row_count": row_count,
        "column_count": len(column_names),
        "columns": [
            profile_column(connection, column_name, row_count)
            for column_name in column_names
        ],
    }


def write_execution_record(record: dict[str, Any], results_path: Path) -> Path:
    results_path.mkdir(parents=True, exist_ok=True)
    record_path = results_path / f"{record['run_id']}.json"
    with record_path.open("x", encoding="utf-8", newline="\n") as output_file:
        json.dump(record, output_file, indent=2, ensure_ascii=False)
        output_file.write("\n")
    return record_path


def run_profiling(config: ProfilingConfig) -> dict[str, Any]:
    """Profile all manifest-declared RAW files and persist the result."""

    timer_started = time.perf_counter()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + f"-{uuid.uuid4().hex[:8]}"
    raw_path = config.raw_path.resolve()
    record: dict[str, Any] = {
        "run_id": run_id,
        "started_at": utc_now(),
        "completed_at": None,
        "elapsed_seconds": None,
        "status": "FAILED",
        "raw_path": str(raw_path),
        "files_expected": 0,
        "files_profiled": 0,
        "tables": [],
    }

    connection: duckdb.DuckDBPyConnection | None = None
    try:
        expected_files = load_manifest(config.manifest_path)
        delimiter, quote = load_physical_format(config.manifest_path)
        record["files_expected"] = len(expected_files)

        missing_files = [
            str(expected["name"])
            for expected in expected_files
            if not (raw_path / str(expected["name"])).is_file()
        ]
        if missing_files:
            raise FileNotFoundError(
                "Missing manifest-declared RAW file(s): " + ", ".join(missing_files)
            )

        connection = duckdb.connect(database=":memory:")
        for expected in expected_files:
            file_name = str(expected["name"])
            LOGGER.info("Profiling %s", file_name)
            table_profile = profile_file(
                connection, raw_path / file_name, delimiter, quote
            )
            record["tables"].append(table_profile)
            record["files_profiled"] += 1
        record["status"] = "SUCCESS"
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        LOGGER.error("Profiling failed: %s", record["error"])
    finally:
        if connection is not None:
            connection.close()
        record["completed_at"] = utc_now()
        record["elapsed_seconds"] = round(time.perf_counter() - timer_started, 6)
        try:
            execution_path = write_execution_record(record, config.results_path)
            record["execution_record"] = str(execution_path.resolve())
        except Exception as exc:
            LOGGER.error("Could not write profiling record: %s", exc)
            record["execution_record"] = "UNAVAILABLE"
            record["status"] = "FAILED"

    return record

