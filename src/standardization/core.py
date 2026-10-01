"""Generic policy validation, transformation, and atomic TRUSTED publication."""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
import shutil
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from src.ingestion.core import load_manifest

LOGGER = logging.getLogger(__name__)
SAMPLE_SIZE = 5


@dataclass(frozen=True)
class StandardizationConfig:
    manifest_path: Path
    catalog_path: Path
    policies_path: Path
    raw_path: Path
    trusted_path: Path
    results_path: Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as yaml_file:
        return yaml.safe_load(yaml_file)


def load_catalog(catalog_path: Path) -> dict[str, dict[str, Any]]:
    catalog: dict[str, dict[str, Any]] = {}
    for path in sorted(catalog_path.glob("*.yaml")):
        document = load_yaml(path)
        asset = document["asset"]
        catalog[asset["name"]] = {
            "source_file": asset["source_file"],
            "columns": {
                column["name"]: column for column in document["columns"]
            },
        }
    return catalog


def load_physical_format(manifest_path: Path) -> tuple[str, str]:
    manifest = load_yaml(manifest_path)
    try:
        physical = manifest["dataset"]["physical_format"]
        delimiter = str(physical["delimiter"])
        quote = str(physical["text_qualifier"])
        has_header = physical["header"]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            "Manifest must define physical_format delimiter, text_qualifier, and header"
        ) from exc
    if len(delimiter) != 1 or len(quote) != 1:
        raise ValueError("Delimiter and text qualifier must contain one character")
    if has_header is not True:
        raise ValueError("Standardization requires the manifest header setting to be true")
    return delimiter, quote


def load_policies(policies_path: Path) -> list[dict[str, Any]]:
    policies: list[dict[str, Any]] = []
    for path in sorted(policies_path.glob("*.yaml")):
        document = load_yaml(path)
        if not isinstance(document, dict) or not isinstance(document.get("policies"), list):
            raise ValueError(f"{path.name} must define a policies list")
        policies.extend(document["policies"])
    if not policies:
        raise ValueError("No standardization policies were found")
    return policies


def validate_policies(
    policies: list[dict[str, Any]],
    catalog: dict[str, dict[str, Any]],
    manifest_files: set[str],
) -> list[str]:
    errors: list[str] = []
    policy_ids = [policy.get("id") for policy in policies if isinstance(policy, dict)]
    for policy_id, count in Counter(policy_ids).items():
        if policy_id and count > 1:
            errors.append(f"Duplicate policy ID: {policy_id}")

    required = ("id", "asset", "column", "transformation", "evidence")
    for index, policy in enumerate(policies):
        if not isinstance(policy, dict):
            errors.append(f"Policy {index} must be a mapping")
            continue
        policy_id = policy.get("id") or f"<policy-{index}>"
        missing = [field for field in required if field not in policy]
        if missing:
            errors.append(f"{policy_id}: missing required fields: {', '.join(missing)}")
            continue
        if not isinstance(policy["id"], str) or not policy["id"]:
            errors.append(f"{policy_id}: policy ID must be non-empty text")

        asset_name = policy["asset"]
        column_name = policy["column"]
        asset = catalog.get(asset_name)
        if asset is None:
            errors.append(f"{policy_id}: unknown catalog asset {asset_name}")
        else:
            if asset["source_file"] not in manifest_files:
                errors.append(
                    f"{policy_id}: catalog source_file {asset['source_file']} is not in the manifest"
                )
            if column_name not in asset["columns"]:
                errors.append(f"{policy_id}: unknown catalog column {asset_name}.{column_name}")

        transformation = policy["transformation"]
        if not isinstance(transformation, dict):
            errors.append(f"{policy_id}: transformation must be a mapping")
        elif transformation.get("type") != "regex_replace":
            errors.append(
                f"{policy_id}: unsupported transformation type {transformation.get('type')}"
            )
        else:
            pattern = transformation.get("pattern")
            replacement = transformation.get("replacement")
            if not isinstance(pattern, str) or not pattern:
                errors.append(f"{policy_id}: regex pattern must be non-empty text")
            else:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    errors.append(f"{policy_id}: invalid regex pattern: {exc}")
            if not isinstance(replacement, str):
                errors.append(f"{policy_id}: replacement must be text")

        evidence = policy["evidence"]
        if not isinstance(evidence, dict) or evidence.get("type") != "catalog":
            errors.append(f"{policy_id}: only catalog evidence is supported")
        else:
            expected_reference = f"{asset_name}.{column_name}"
            if evidence.get("reference") != expected_reference:
                errors.append(
                    f"{policy_id}: catalog evidence must reference {expected_reference}"
                )
            if not isinstance(evidence.get("description"), str) or not evidence["description"]:
                errors.append(f"{policy_id}: evidence description must be non-empty text")
            if asset is not None and column_name in asset["columns"]:
                if not asset["columns"][column_name].get("documented_format"):
                    errors.append(
                        f"{policy_id}: catalog column {expected_reference} has no documented_format"
                    )
    return errors


def split_line_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n") or line.endswith("\r"):
        return line[:-1], line[-1:]
    return line, ""


def split_physical_fields(record: str, delimiter: str, quote: str) -> list[str]:
    """Split one physical CSV record while preserving each original token."""

    fields: list[str] = []
    start = 0
    in_quotes = False
    index = 0
    while index < len(record):
        character = record[index]
        if character == quote:
            if in_quotes and index + 1 < len(record) and record[index + 1] == quote:
                index += 2
                continue
            in_quotes = not in_quotes
        elif character == delimiter and not in_quotes:
            fields.append(record[start:index])
            start = index + 1
        index += 1
    if in_quotes:
        raise ValueError("Unterminated quoted field")
    fields.append(record[start:])
    return fields


def encode_field(value: str, original_token: str, delimiter: str, quote: str) -> str:
    originally_quoted = (
        len(original_token) >= 2
        and original_token.startswith(quote)
        and original_token.endswith(quote)
    )
    needs_quote = any(character in value for character in (delimiter, quote, "\r", "\n"))
    if originally_quoted or needs_quote:
        return quote + value.replace(quote, quote + quote) + quote
    return value


def update_samples(samples: list[tuple[str, str]], before: str, after: str) -> None:
    change = (before, after)
    if change not in samples:
        samples.append(change)
        samples.sort()
        del samples[SAMPLE_SIZE:]


def transform_asset(
    raw_file: Path,
    staging_file: Path,
    policies: list[dict[str, Any]],
    delimiter: str,
    quote: str,
) -> list[dict[str, Any]]:
    metrics = {
        policy["id"]: {
            "policy_id": policy["id"],
            "asset": policy["asset"],
            "column": policy["column"],
            "transformation_type": policy["transformation"]["type"],
            "rows_total": 0,
            "rows_evaluated": 0,
            "rows_changed": 0,
            "rows_unchanged": 0,
            "_samples": [],
        }
        for policy in policies
    }
    compiled = {
        policy["id"]: re.compile(policy["transformation"]["pattern"])
        for policy in policies
    }

    with raw_file.open("r", encoding="utf-8", newline="") as source, staging_file.open(
        "w", encoding="utf-8", newline=""
    ) as target:
        header_line = source.readline()
        if not header_line:
            raise ValueError(f"RAW file {raw_file.name} has no header")
        header_record, _ = split_line_ending(header_line)
        header = next(csv.reader([header_record], delimiter=delimiter, quotechar=quote))
        column_indexes = {name: index for index, name in enumerate(header)}
        for policy in policies:
            if policy["column"] not in column_indexes:
                raise ValueError(
                    f"RAW header {raw_file.name} has no column {policy['column']}"
                )
        target.write(header_line)

        for line_number, line in enumerate(source, start=2):
            record, ending = split_line_ending(line)
            physical_fields = split_physical_fields(record, delimiter, quote)
            values = next(csv.reader([record], delimiter=delimiter, quotechar=quote))
            if len(values) != len(header) or len(physical_fields) != len(header):
                raise ValueError(
                    f"{raw_file.name} line {line_number} has an unexpected column count"
                )
            for policy in policies:
                result = metrics[policy["id"]]
                result["rows_total"] += 1
                result["rows_evaluated"] += 1
                index = column_indexes[policy["column"]]
                before = values[index]
                match = compiled[policy["id"]].fullmatch(before)
                after = match.expand(policy["transformation"]["replacement"]) if match else before
                if after != before:
                    result["rows_changed"] += 1
                    update_samples(result["_samples"], before, after)
                    values[index] = after
                    physical_fields[index] = encode_field(
                        after, physical_fields[index], delimiter, quote
                    )
                else:
                    result["rows_unchanged"] += 1
            target.write(delimiter.join(physical_fields) + ending)

    results: list[dict[str, Any]] = []
    for policy in policies:
        result = metrics[policy["id"]]
        result["sample_changes"] = [
            {"before": before, "after": after}
            for before, after in result.pop("_samples")
        ]
        results.append(result)
    return results


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def header_and_rows(path: Path, delimiter: str, quote: str) -> tuple[list[str], int]:
    with path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.reader(file, delimiter=delimiter, quotechar=quote)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise ValueError(f"{path.name} has no header") from exc
        return header, sum(1 for _ in reader)


def validate_staging(
    raw_path: Path,
    staging_path: Path,
    manifest_files: list[str],
    transformed_files: set[str],
    delimiter: str,
    quote: str,
) -> None:
    for file_name in manifest_files:
        raw_file = raw_path / file_name
        staged_file = staging_path / file_name
        if not staged_file.is_file():
            raise ValueError(f"Staging is missing expected asset {file_name}")
        if file_name not in transformed_files:
            if sha256(raw_file) != sha256(staged_file):
                raise ValueError(f"Copied asset is not byte-identical: {file_name}")
        else:
            raw_header, raw_rows = header_and_rows(raw_file, delimiter, quote)
            staged_header, staged_rows = header_and_rows(staged_file, delimiter, quote)
            if raw_header != staged_header or raw_rows != staged_rows:
                raise ValueError(
                    f"Transformed asset structure or row count changed: {file_name}"
                )


def build_and_publish(
    raw_path: Path,
    trusted_path: Path,
    manifest_files: list[str],
    catalog: dict[str, dict[str, Any]],
    policies: list[dict[str, Any]],
    delimiter: str,
    quote: str,
) -> tuple[int, int, list[dict[str, Any]]]:
    for file_name in manifest_files:
        if not (raw_path / file_name).is_file():
            raise FileNotFoundError(f"Required RAW asset is missing: {file_name}")

    policies_by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for policy in policies:
        policies_by_file[catalog[policy["asset"]]["source_file"]].append(policy)

    trusted_path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    staging_path = trusted_path.parent / f".{trusted_path.name}-staging-{token}"
    backup_path = trusted_path.parent / f".{trusted_path.name}-backup-{token}"
    previous_moved = False
    policy_results: list[dict[str, Any]] = []
    assets_copied = 0
    assets_transformed = 0
    try:
        staging_path.mkdir()
        if (trusted_path / ".gitkeep").is_file():
            shutil.copy2(trusted_path / ".gitkeep", staging_path / ".gitkeep")
        for file_name in manifest_files:
            source_file = raw_path / file_name
            target_file = staging_path / file_name
            asset_policies = policies_by_file.get(file_name, [])
            if asset_policies:
                policy_results.extend(
                    transform_asset(
                        source_file, target_file, asset_policies, delimiter, quote
                    )
                )
                assets_transformed += 1
            else:
                shutil.copy2(source_file, target_file)
                assets_copied += 1

        validate_staging(
            raw_path,
            staging_path,
            manifest_files,
            set(policies_by_file),
            delimiter,
            quote,
        )
        if trusted_path.exists():
            trusted_path.replace(backup_path)
            previous_moved = True
        staging_path.replace(trusted_path)
        if previous_moved:
            shutil.rmtree(backup_path)
        return assets_copied, assets_transformed, policy_results
    except Exception:
        if previous_moved and backup_path.exists():
            if trusted_path.exists():
                shutil.rmtree(trusted_path)
            backup_path.replace(trusted_path)
        raise
    finally:
        if staging_path.exists():
            shutil.rmtree(staging_path)
        if backup_path.exists() and trusted_path.exists():
            shutil.rmtree(backup_path)


def write_execution_record(record: dict[str, Any], results_path: Path) -> Path:
    results_path.mkdir(parents=True, exist_ok=True)
    path = results_path / f"{record['run_id']}.json"
    with path.open("x", encoding="utf-8", newline="\n") as output_file:
        json.dump(record, output_file, indent=2, ensure_ascii=False)
        output_file.write("\n")
    return path


def run_standardization(config: StandardizationConfig) -> dict[str, Any]:
    timer_started = time.perf_counter()
    run_id = "std-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + f"-{uuid.uuid4().hex[:8]}"
    record: dict[str, Any] = {
        "run_id": run_id,
        "started_at": utc_now(),
        "completed_at": None,
        "elapsed_seconds": None,
        "status": "FAILED",
        "policies_total": 0,
        "policies_applied": 0,
        "assets_total": 0,
        "assets_copied": 0,
        "assets_transformed": 0,
        "errors": [],
        "policy_results": [],
        "trusted_path": str(config.trusted_path.resolve()),
    }
    try:
        manifest_entries = load_manifest(config.manifest_path)
        manifest_files = [str(entry["name"]) for entry in manifest_entries]
        record["assets_total"] = len(manifest_files)
        delimiter, quote = load_physical_format(config.manifest_path)
        catalog = load_catalog(config.catalog_path)
        policies = load_policies(config.policies_path)
        record["policies_total"] = len(policies)
        policy_errors = validate_policies(policies, catalog, set(manifest_files))
        if policy_errors:
            record["errors"] = policy_errors
            raise ValueError(f"Policy validation failed with {len(policy_errors)} error(s)")

        copied, transformed, policy_results = build_and_publish(
            config.raw_path,
            config.trusted_path,
            manifest_files,
            catalog,
            policies,
            delimiter,
            quote,
        )
        record["policies_applied"] = len(policy_results)
        record["assets_copied"] = copied
        record["assets_transformed"] = transformed
        record["policy_results"] = policy_results
        record["status"] = "SUCCESS"
    except Exception as exc:
        if not record["errors"]:
            record["errors"] = [f"{type(exc).__name__}: {exc}"]
        LOGGER.error("Standardization failed: %s", exc)
    finally:
        record["completed_at"] = utc_now()
        record["elapsed_seconds"] = round(time.perf_counter() - timer_started, 6)
        try:
            path = write_execution_record(record, config.results_path)
            record["execution_record"] = str(path.resolve())
        except Exception as exc:
            LOGGER.error("Could not write standardization record: %s", exc)
            record["execution_record"] = "UNAVAILABLE"
            record["status"] = "FAILED"
    return record

