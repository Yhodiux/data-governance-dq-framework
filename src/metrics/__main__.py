"""Command-line entry point for DQ metrics."""

from pathlib import Path

from .core import MetricsConfig, run_metrics


def main() -> int:
    result = run_metrics(MetricsConfig(Path(".")))
    print(result["status"])
    print(f"evaluations={result['evaluations']}")
    print(f"aggregates={result['aggregates']}")
    print(f"database={result['database_path']}")
    for error in result["errors"]:
        print(error)
    return 0 if result["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
