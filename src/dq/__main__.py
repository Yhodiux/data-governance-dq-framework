"""Command-line entry point for local Data Quality execution."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .core import DQConfig, run_dq


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Execute metadata-driven Data Quality rules against a data zone."
    )
    parser.add_argument(
        "--data-path",
        type=Path,
        default=Path("data/raw"),
        help="Physical directory containing the dataset (default: data/raw).",
    )
    parser.add_argument(
        "--data-zone",
        choices=("raw", "trusted"),
        default="raw",
        help="Semantic zone label recorded with the execution (default: raw).",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    execution = run_dq(
        DQConfig(
            manifest_path=Path("config/dataset_manifest.yaml"),
            catalog_path=Path("metadata/catalog"),
            relationships_path=Path("metadata/relationships.yaml"),
            rules_path=Path("metadata/dq_rules"),
            data_path=args.data_path,
            data_zone=args.data_zone,
            results_path=Path("data/results/dq"),
        )
    )
    logging.info(
        "DQ execution %s: %s zone=%s, %d passed, %d failed (%s)",
        execution["run_id"],
        execution["status"],
        execution["data_zone"],
        execution["rules_passed"],
        execution["rules_failed"],
        execution["execution_record"],
    )
    return 0 if execution["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
