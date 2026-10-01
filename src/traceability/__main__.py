"""Command-line entry point for execution traceability."""

from pathlib import Path

from .core import TraceabilityConfig, run_traceability


def main() -> int:
    result = run_traceability(TraceabilityConfig(Path(".")))
    print(f"{result['status']}: runs={result['runs']}, metrics={result['metrics']}, details={result['details']}")
    print(result["database_path"])
    for error in result["errors"]:
        print(error)
    return 0 if result["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
