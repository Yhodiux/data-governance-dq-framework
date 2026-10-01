"""Build the local structural lineage projection."""

from pathlib import Path

from .core import LineageConfig, run_lineage


def main() -> int:
    record = run_lineage(LineageConfig(root_path=Path(".")))
    print(f"{record['status']}: nodes={record['nodes_generated']}, edges={record['edges_generated']}")
    print(record["execution_record"])
    for error in record["errors"]:
        print(error)
    return 0 if record["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
