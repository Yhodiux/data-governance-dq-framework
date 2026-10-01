"""Command-line entry point for rebuilding local DQ history."""

from __future__ import annotations

import logging
from pathlib import Path

from .core import ObservabilityConfig, run_observability_build


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    execution = run_observability_build(
        ObservabilityConfig(
            source_path=Path("data/results/dq"),
            database_path=Path("data/results/observability/dq_history.duckdb"),
            runs_path=Path("data/results/observability/runs"),
        )
    )
    logging.info(
        "Observability build %s: %s; discovered=%d, runs=%d, rule_results=%d",
        execution["run_id"],
        execution["status"],
        execution["source_records_discovered"],
        execution["dq_runs_loaded"],
        execution["dq_rule_results_loaded"],
    )
    logging.info("Database: %s", execution["database_path"])
    logging.info("Build record: %s", execution["execution_record"])
    for error in execution["errors"]:
        logging.error("%s: %s", error["source_record"], error["message"])
    return 0 if execution["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

