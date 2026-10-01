"""Validation, publication, and execution reporting for local ingestion."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestionConfig:
    """Filesystem locations used by one ingestion run."""

    source_path: Path
    manifest_path: Path
    raw_path: Path
    results_path: Path


def utc_now() -> str:
    """Return an unambiguous UTC timestamp."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def calculate_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Calculate a file checksum without loading the full file into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def count_data_records(path: Path) -> int:
    """Count physical records after the first, header record."""

    with path.open("rb") as source_file:
        line_count = sum(1 for _ in source_file)
    return max(0, line_count - 1)


def load_manifest(path: Path) -> list[dict[str, Any]]:
    """Load and minimally validate the manifest entries needed by ingestion."""

    with path.open("r", encoding="utf-8") as manifest_file:
        manifest = yaml.safe_load(manifest_file)

    try:
        files = manifest["dataset"]["files"]
    except (KeyError, TypeError) as exc:
        raise ValueError("Manifest must define dataset.files") from exc

    if not isinstance(files, list) or not files:
        raise ValueError("Manifest dataset.files must be a non-empty list")

    required_fields = {"name", "sha256", "expected_record_count"}
    for index, entry in enumerate(files):
        if not isinstance(entry, dict) or not required_fields.issubset(entry):
            raise ValueError(
                f"Manifest file entry {index} must define name, sha256, "
                "and expected_record_count"
            )
        if Path(str(entry["name"])).name != str(entry["name"]):
            raise ValueError(f"Manifest file entry {index} must use a file name only")

    return files


def validate_file(source_path: Path, expected: dict[str, Any]) -> dict[str, Any]:
    """Measure one source file and compare it with its manifest baseline."""

    file_name = str(expected["name"])
    expected_sha256 = str(expected["sha256"]).upper()
    expected_record_count = int(expected["expected_record_count"])
    result: dict[str, Any] = {
        "file_name": file_name,
        "expected_sha256": expected_sha256,
        "expected_record_count": expected_record_count,
        "validation_status": "FAILED",
    }
    file_path = source_path / file_name

    if not file_path.is_file():
        result["message"] = "Source file is missing"
        return result

    errors: list[str] = []
    try:
        actual_sha256 = calculate_sha256(file_path)
        result["actual_sha256"] = actual_sha256
        if actual_sha256 != expected_sha256:
            errors.append("SHA-256 mismatch")

        actual_record_count = count_data_records(file_path)
        result["actual_record_count"] = actual_record_count
        if actual_record_count != expected_record_count:
            errors.append("Record-count mismatch")
    except (OSError, PermissionError) as exc:
        errors.append(f"Unable to read source file: {exc}")

    if errors:
        result["message"] = "; ".join(errors)
    else:
        result["validation_status"] = "PASSED"
    return result


def validate_source(
    source_path: Path, expected_files: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Validate every manifest-declared source file."""

    return [validate_file(source_path, expected) for expected in expected_files]


def publish_atomically(source_path: Path, raw_path: Path, file_names: list[str]) -> None:
    """Stage exact copies and replace RAW as one directory-level publication."""

    raw_parent = raw_path.parent
    raw_parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    staging_path = raw_parent / f".{raw_path.name}-staging-{token}"
    backup_path = raw_parent / f".{raw_path.name}-backup-{token}"
    previous_raw_moved = False

    try:
        staging_path.mkdir()
        if (raw_path / ".gitkeep").is_file():
            shutil.copy2(raw_path / ".gitkeep", staging_path / ".gitkeep")
        for file_name in file_names:
            shutil.copy2(source_path / file_name, staging_path / file_name)

        if raw_path.exists():
            raw_path.replace(backup_path)
            previous_raw_moved = True
        staging_path.replace(raw_path)

        if previous_raw_moved:
            shutil.rmtree(backup_path)
    except Exception:
        if previous_raw_moved and backup_path.exists():
            if raw_path.exists():
                shutil.rmtree(raw_path)
            backup_path.replace(raw_path)
        raise
    finally:
        if staging_path.exists():
            shutil.rmtree(staging_path)
        if backup_path.exists() and raw_path.exists():
            shutil.rmtree(backup_path)


def write_execution_record(record: dict[str, Any], results_path: Path) -> Path:
    """Persist one execution record using its unique run identifier."""

    results_path.mkdir(parents=True, exist_ok=True)
    record_path = results_path / f"{record['run_id']}.json"
    with record_path.open("x", encoding="utf-8", newline="\n") as output_file:
        json.dump(record, output_file, indent=2, ensure_ascii=False)
        output_file.write("\n")
    return record_path


def run_ingestion(config: IngestionConfig) -> dict[str, Any]:
    """Run manifest validation, atomic RAW publication, and execution reporting."""

    started_at = utc_now()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + f"-{uuid.uuid4().hex[:8]}"
    source_path = config.source_path.resolve()
    raw_path = config.raw_path.resolve()
    record: dict[str, Any] = {
        "run_id": run_id,
        "started_at": started_at,
        "completed_at": None,
        "status": "FAILED",
        "source_path": str(source_path),
        "raw_path": str(raw_path),
        "files_expected": 0,
        "files_processed": 0,
        "files": [],
    }

    try:
        expected_files = load_manifest(config.manifest_path)
        record["files_expected"] = len(expected_files)
        record["files"] = validate_source(source_path, expected_files)
        record["files_processed"] = len(record["files"])

        failed_files = [
            item for item in record["files"] if item["validation_status"] != "PASSED"
        ]
        if failed_files:
            record["error"] = f"Source validation failed for {len(failed_files)} file(s)"
            LOGGER.error(record["error"])
        else:
            publish_atomically(
                source_path,
                raw_path,
                [str(expected["name"]) for expected in expected_files],
            )
            record["status"] = "SUCCESS"
            LOGGER.info("Published %d validated files to %s", len(expected_files), raw_path)
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        LOGGER.error("Ingestion failed: %s", record["error"])
    finally:
        record["completed_at"] = utc_now()
        try:
            execution_path = write_execution_record(record, config.results_path)
            record["execution_record"] = str(execution_path.resolve())
        except Exception as exc:
            LOGGER.error("Could not write execution record: %s", exc)
            record["execution_record"] = "UNAVAILABLE"
            record["status"] = "FAILED"

    return record

