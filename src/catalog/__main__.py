"""Command-line entry point for metadata catalog validation."""

from __future__ import annotations

import logging
from pathlib import Path

from .core import CatalogConfig, run_catalog_validation


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    execution = run_catalog_validation(
        CatalogConfig(
            manifest_path=Path("config/dataset_manifest.yaml"),
            catalog_path=Path("metadata/catalog"),
            relationships_path=Path("metadata/relationships.yaml"),
            raw_path=Path("data/raw"),
            results_path=Path("data/results/catalog"),
        )
    )
    logging.info(
        "Catalog validation %s: %s (%s)",
        execution["run_id"],
        execution["status"],
        execution["execution_record"],
    )
    return 0 if execution["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

