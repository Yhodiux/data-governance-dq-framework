"""Command-line entry point for local ingestion."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .core import IngestionConfig, run_ingestion


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate the configured source dataset and publish it to RAW."
    )
    parser.add_argument(
        "--source",
        required=True,
        type=Path,
        help="Directory containing the external source files.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    config = IngestionConfig(
        source_path=args.source,
        manifest_path=Path("config/dataset_manifest.yaml"),
        raw_path=Path("data/raw"),
        results_path=Path("data/results/ingestion"),
    )
    execution = run_ingestion(config)
    logging.info(
        "Ingestion %s: %s (%s)",
        execution["run_id"],
        execution["status"],
        execution["execution_record"],
    )
    return 0 if execution["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

