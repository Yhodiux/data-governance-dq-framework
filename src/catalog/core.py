"""Internal and RAW-header consistency validation for the metadata catalog."""

from __future__ import annotations

import csv
import json
import logging
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from src.ingestion.core import load_manifest

LOGGER = logging.getLogger(__name__)

CHECK_NAMES = (
    "catalog_structure",
    "manifest_asset_coverage",
    "source_file_declaration",
    "unique_asset_names",
    "unique_columns",
    "raw_headers_available",
    "catalog_columns_in_raw",
    "raw_columns_in_catalog",
    "relationship_structure",
    "relationship_assets",
    "relationship_columns",
)


@dataclass(frozen=True)
class CatalogConfig:
    manifest_path: Path
    catalog_path: Path
    relationships_path: Path
    raw_path: Path
    results_path: Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def add_error(errors: list[dict[str, str]], check: str, message: str) -> None:
    errors.append({"check": check, "message": message})


def valid_evidence(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("type"), str)
        and bool(value["type"])
        and isinstance(value.get("document"), str)
        and bool(value["document"])
    )


def load_physical_format(manifest_path: Path) -> tuple[str, str]:
    """Load the delimiter and quote character without a profiling dependency."""

    with manifest_path.open("r", encoding="utf-8") as manifest_file:
        manifest = yaml.safe_load(manifest_file)
    try:
        physical_format = manifest["dataset"]["physical_format"]
        delimiter = str(physical_format["delimiter"])
        quote = str(physical_format["text_qualifier"])
        has_header = physical_format["header"]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            "Manifest must define physical_format delimiter, text_qualifier, and header"
        ) from exc
    if len(delimiter) != 1 or len(quote) != 1:
        raise ValueError("Delimiter and text qualifier must each contain one character")
    if has_header is not True:
        raise ValueError("Catalog validation requires the manifest header setting to be true")
    return delimiter, quote


def load_catalog_assets(
    catalog_path: Path, errors: list[dict[str, str]]
) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    if not catalog_path.is_dir():
        add_error(errors, "catalog_structure", f"Catalog directory is missing: {catalog_path}")
        return assets

    for yaml_path in sorted(catalog_path.glob("*.yaml")):
        try:
            document = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            add_error(errors, "catalog_structure", f"Cannot read {yaml_path.name}: {exc}")
            continue

        if not isinstance(document, dict):
            add_error(errors, "catalog_structure", f"{yaml_path.name} must contain a mapping")
            continue
        asset = document.get("asset")
        columns = document.get("columns")
        if not isinstance(asset, dict):
            add_error(errors, "catalog_structure", f"{yaml_path.name} must define asset")
            continue

        required_asset_fields = ("name", "source_file", "description", "evidence")
        missing_asset_fields = [field for field in required_asset_fields if not asset.get(field)]
        if missing_asset_fields:
            add_error(
                errors,
                "catalog_structure",
                f"{yaml_path.name} asset is missing: {', '.join(missing_asset_fields)}",
            )
        if not valid_evidence(asset.get("evidence")):
            add_error(errors, "catalog_structure", f"{yaml_path.name} asset evidence is invalid")
        if not isinstance(columns, list) or not columns:
            add_error(errors, "catalog_structure", f"{yaml_path.name} columns must be a non-empty list")
            columns = []

        for index, column in enumerate(columns):
            if not isinstance(column, dict):
                add_error(errors, "catalog_structure", f"{yaml_path.name} column {index} must be a mapping")
                continue
            missing_column_fields = [
                field for field in ("name", "description", "evidence") if not column.get(field)
            ]
            if missing_column_fields:
                add_error(
                    errors,
                    "catalog_structure",
                    f"{yaml_path.name} column {index} is missing: {', '.join(missing_column_fields)}",
                )
            if not valid_evidence(column.get("evidence")):
                add_error(
                    errors,
                    "catalog_structure",
                    f"{yaml_path.name} column {index} evidence is invalid",
                )
            if "documented_format" in column and not isinstance(column["documented_format"], str):
                add_error(errors, "catalog_structure", f"{yaml_path.name} column {index} documented_format must be text")
            if "documented_values" in column and not isinstance(column["documented_values"], dict):
                add_error(errors, "catalog_structure", f"{yaml_path.name} column {index} documented_values must be a mapping")

        assets.append(
            {
                "catalog_file": yaml_path.name,
                "name": asset.get("name"),
                "source_file": asset.get("source_file"),
                "columns": columns,
            }
        )
    return assets


def read_raw_header(path: Path, delimiter: str, quote: str) -> list[str]:
    """Read only the first physical CSV record from RAW."""

    with path.open("r", encoding="utf-8-sig", newline="") as raw_file:
        reader = csv.reader(raw_file, delimiter=delimiter, quotechar=quote)
        try:
            return next(reader)
        except StopIteration as exc:
            raise ValueError("RAW file is empty and has no header") from exc


def load_relationships(
    relationships_path: Path, errors: list[dict[str, str]]
) -> list[dict[str, Any]]:
    try:
        document = yaml.safe_load(relationships_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        add_error(errors, "relationship_structure", f"Cannot read relationships: {exc}")
        return []

    if not isinstance(document, dict) or not isinstance(document.get("relationships"), list):
        add_error(errors, "relationship_structure", "relationships.yaml must define a relationships list")
        return []

    relationships = document["relationships"]
    names: list[str] = []
    for index, relationship in enumerate(relationships):
        if not isinstance(relationship, dict):
            add_error(errors, "relationship_structure", f"Relationship {index} must be a mapping")
            continue
        names.append(relationship.get("name"))
        for field in ("name", "from", "to", "evidence"):
            if not relationship.get(field):
                add_error(errors, "relationship_structure", f"Relationship {index} is missing {field}")
        for endpoint_name in ("from", "to"):
            endpoint = relationship.get(endpoint_name)
            if not isinstance(endpoint, dict) or not endpoint.get("asset") or not endpoint.get("column"):
                add_error(
                    errors,
                    "relationship_structure",
                    f"Relationship {index} {endpoint_name} must define asset and column",
                )
        if not valid_evidence(relationship.get("evidence")):
            add_error(errors, "relationship_structure", f"Relationship {index} evidence is invalid")

    duplicate_names = [name for name, count in Counter(names).items() if name and count > 1]
    for name in duplicate_names:
        add_error(errors, "relationship_structure", f"Duplicate relationship name: {name}")
    return relationships


def validate_catalog(config: CatalogConfig) -> dict[str, Any]:
    """Validate catalog structure, manifest coverage, RAW headers, and relationships."""

    errors: list[dict[str, str]] = []
    expected_files = load_manifest(config.manifest_path)
    delimiter, quote = load_physical_format(config.manifest_path)
    manifest_names = [str(entry["name"]) for entry in expected_files]
    manifest_set = set(manifest_names)
    assets = load_catalog_assets(config.catalog_path, errors)

    source_counts = Counter(asset["source_file"] for asset in assets if asset["source_file"])
    for source_file in manifest_names:
        if source_counts[source_file] != 1:
            add_error(
                errors,
                "manifest_asset_coverage",
                f"Expected exactly one catalog asset for {source_file}; found {source_counts[source_file]}",
            )
    for asset in assets:
        if asset["source_file"] not in manifest_set:
            add_error(
                errors,
                "source_file_declaration",
                f"Catalog asset {asset['name']} references undeclared source_file {asset['source_file']}",
            )

    asset_names = [asset["name"] for asset in assets if asset["name"]]
    for name, count in Counter(asset_names).items():
        if count > 1:
            add_error(errors, "unique_asset_names", f"Duplicate asset name: {name}")

    assets_by_name: dict[str, dict[str, Any]] = {}
    for asset in assets:
        if asset["name"] and asset["name"] not in assets_by_name:
            assets_by_name[asset["name"]] = asset
        column_names = [
            column.get("name")
            for column in asset["columns"]
            if isinstance(column, dict) and column.get("name")
        ]
        for column_name, count in Counter(column_names).items():
            if count > 1:
                add_error(
                    errors,
                    "unique_columns",
                    f"Asset {asset['name']} has duplicate column {column_name}",
                )

        source_file = asset["source_file"]
        if source_file not in manifest_set:
            continue
        raw_file = config.raw_path / source_file
        if not raw_file.is_file():
            add_error(errors, "raw_headers_available", f"RAW file is missing: {source_file}")
            continue
        try:
            raw_columns = read_raw_header(raw_file, delimiter, quote)
        except (OSError, csv.Error, UnicodeError, ValueError) as exc:
            add_error(errors, "raw_headers_available", f"Cannot read header for {source_file}: {exc}")
            continue

        catalog_column_set = set(column_names)
        raw_column_set = set(raw_columns)
        for column_name in sorted(catalog_column_set - raw_column_set):
            add_error(
                errors,
                "catalog_columns_in_raw",
                f"Catalog column {asset['name']}.{column_name} is absent from RAW header",
            )
        for column_name in sorted(raw_column_set - catalog_column_set):
            add_error(
                errors,
                "raw_columns_in_catalog",
                f"RAW column {source_file}.{column_name} is absent from catalog",
            )

    relationships = load_relationships(config.relationships_path, errors)
    for relationship in relationships:
        if not isinstance(relationship, dict):
            continue
        for endpoint_name in ("from", "to"):
            endpoint = relationship.get(endpoint_name)
            if not isinstance(endpoint, dict):
                continue
            asset_name = endpoint.get("asset")
            column_name = endpoint.get("column")
            if asset_name not in assets_by_name:
                add_error(
                    errors,
                    "relationship_assets",
                    f"Relationship {relationship.get('name')} references unknown asset {asset_name}",
                )
                continue
            catalog_columns = {
                column.get("name")
                for column in assets_by_name[asset_name]["columns"]
                if isinstance(column, dict)
            }
            if column_name not in catalog_columns:
                add_error(
                    errors,
                    "relationship_columns",
                    f"Relationship {relationship.get('name')} references unknown column {asset_name}.{column_name}",
                )

    failed_checks = {error["check"] for error in errors}
    return {
        "assets_expected": len(manifest_names),
        "assets_validated": len(assets),
        "relationships_validated": len(relationships),
        "checks": [
            {"name": name, "status": "FAILED" if name in failed_checks else "PASSED"}
            for name in CHECK_NAMES
        ],
        "errors": errors,
    }


def write_execution_record(record: dict[str, Any], results_path: Path) -> Path:
    results_path.mkdir(parents=True, exist_ok=True)
    record_path = results_path / f"{record['run_id']}.json"
    with record_path.open("x", encoding="utf-8", newline="\n") as output_file:
        json.dump(record, output_file, indent=2, ensure_ascii=False)
        output_file.write("\n")
    return record_path


def run_catalog_validation(config: CatalogConfig) -> dict[str, Any]:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + f"-{uuid.uuid4().hex[:8]}"
    record: dict[str, Any] = {
        "run_id": run_id,
        "started_at": utc_now(),
        "completed_at": None,
        "status": "FAILED",
        "assets_expected": 0,
        "assets_validated": 0,
        "relationships_validated": 0,
        "checks": [],
        "errors": [],
    }
    try:
        validation = validate_catalog(config)
        record.update(validation)
        if not record["errors"]:
            record["status"] = "SUCCESS"
        else:
            LOGGER.error("Catalog validation found %d error(s)", len(record["errors"]))
    except Exception as exc:
        record["errors"] = [
            {"check": "execution", "message": f"{type(exc).__name__}: {exc}"}
        ]
        LOGGER.error("Catalog validation failed: %s", record["errors"][0]["message"])
    finally:
        record["completed_at"] = utc_now()
        try:
            execution_path = write_execution_record(record, config.results_path)
            record["execution_record"] = str(execution_path.resolve())
        except Exception as exc:
            LOGGER.error("Could not write catalog execution record: %s", exc)
            record["execution_record"] = "UNAVAILABLE"
            record["status"] = "FAILED"
    return record
