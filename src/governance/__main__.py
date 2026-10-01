"""Command-line entry point for the governance registry."""

from pathlib import Path

from .core import GovernanceConfig, run_governance


def main() -> int:
    result = run_governance(GovernanceConfig(Path(".")))
    print(f"{result['status']}: issues={result['issues']}, decisions={result['decisions']}, evidence={result['evidence']}")
    print(result["database_path"])
    for error in result["errors"]:
        print(error)
    return 0 if result["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
