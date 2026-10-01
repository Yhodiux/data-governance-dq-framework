"""Command-line entry point for local Data Quality execution."""

from __future__ import annotations

import logging
from pathlib import Path

from .core import DQConfig, run_dq


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    execution = run_dq(
        DQConfig(
            manifest_path=Path("config/dataset_manifest.yaml"),
            catalog_path=Path("metadata/catalog"),
            relationships_path=Path("metadata/relationships.yaml"),
            rules_path=Path("metadata/dq_rules"),
            raw_path=Path("data/raw"),
            results_path=Path("data/results/dq"),
        )
    )
    logging.info(
        "DQ execution %s: %s, %d passed, %d failed (%s)",
        execution["run_id"],
        execution["status"],
        execution["rules_passed"],
        execution["rules_failed"],
        execution["execution_record"],
    )
    return 0 if execution["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

