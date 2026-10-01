"""Local historical snapshot command."""

import argparse
from pathlib import Path

from .core import ReplayConfig, run_replay


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a historical snapshot from TRUSTED.")
    parser.add_argument("--cutoff", required=True, help="Inclusive ISO date YYYY-MM-DD")
    args = parser.parse_args()
    record = run_replay(ReplayConfig(
        manifest_path=Path("config/dataset_manifest.yaml"),
        catalog_path=Path("metadata/catalog"),
        relationships_path=Path("metadata/relationships.yaml"),
        policy_path=Path("metadata/replay/historical_snapshot.yaml"),
        source_path=Path("data/trusted"),
        snapshots_path=Path("data/snapshots"),
        results_path=Path("data/results/replay"),
        cutoff=args.cutoff,
    ))
    print(f"{record['status']}: {record['execution_record']}")
    for error in record["errors"]:
        print(error)
    return 0 if record["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
