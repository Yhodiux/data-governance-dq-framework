"""Command-line entry point for local profiling."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .core import ProfilingConfig, run_profiling


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure physical characteristics of the local RAW dataset."
    )
    parser.add_argument(
        "--raw",
        type=Path,
        default=Path("data/raw"),
        help="RAW directory to profile (default: data/raw).",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    execution = run_profiling(
        ProfilingConfig(
            raw_path=args.raw,
            manifest_path=Path("config/dataset_manifest.yaml"),
            results_path=Path("data/results/profiling"),
        )
    )
    logging.info(
        "Profiling %s: %s (%s)",
        execution["run_id"],
        execution["status"],
        execution["execution_record"],
    )
    return 0 if execution["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

