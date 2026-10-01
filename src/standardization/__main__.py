"""Command-line entry point for TRUSTED standardization."""

from __future__ import annotations

import logging
from pathlib import Path

from .core import StandardizationConfig, run_standardization


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    execution = run_standardization(
        StandardizationConfig(
            manifest_path=Path("config/dataset_manifest.yaml"),
            catalog_path=Path("metadata/catalog"),
            policies_path=Path("metadata/standardization"),
            raw_path=Path("data/raw"),
            trusted_path=Path("data/trusted"),
            results_path=Path("data/results/standardization"),
        )
    )
    logging.info(
        "Standardization %s: %s; policies=%d/%d, copied=%d, transformed=%d",
        execution["run_id"],
        execution["status"],
        execution["policies_applied"],
        execution["policies_total"],
        execution["assets_copied"],
        execution["assets_transformed"],
    )
    logging.info("TRUSTED: %s", execution["trusted_path"])
    logging.info("Execution record: %s", execution["execution_record"])
    for error in execution["errors"]:
        logging.error("%s", error)
    return 0 if execution["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

