# Metadata Catalog

## Purpose

Step 2 provides a version-controlled catalog of the Berka assets, columns, documented formats, documented code meanings, and documented relationships. Its purpose is to preserve source definitions and their provenance in machine-readable YAML.

The catalog contains one file under `metadata/catalog/` for each manifest-declared data asset, plus `metadata/relationships.yaml` for cross-asset relationships.

## Catalog schema

Each asset catalog has this small schema:

- `asset.name`: catalog asset identifier.
- `asset.source_file`: corresponding manifest-declared source file.
- `asset.description`: description supported by source documentation.
- `asset.evidence`: evidence `type` and source `document`.
- `columns`: ordered list of physical columns.
- `columns[].name`: physical RAW header name.
- `columns[].description`: documented meaning.
- `columns[].evidence`: evidence `type` and source `document`.
- `columns[].documented_format`: optional, present only when the source documents a format.
- `columns[].documented_values`: optional mapping, present only when the source documents code or value meanings.

Each relationship contains a unique `name`, `from` and `to` endpoints with asset and column names, and documentary `evidence`. It does not claim enforcement or measured referential integrity.

## Provenance and metadata categories

All current definitions use evidence type `source_documentation` and identify `Financial Data description.pdf`. Wording remains close to that source. General banking knowledge is not used to fill gaps.

Three metadata categories remain separate:

1. **Source metadata** records what the supplied documentation says. This is the scope of the catalog YAML.
2. **Observed metadata** records physical measurements produced by profiling. Those results remain in `data/results/profiling/` and are not copied into the catalog.
3. **Governance decisions** include ownership, stewardship, CDEs, sensitivity, criticality, policies, and similar decisions. They are not introduced in Step 2.

## Catalog validation

`python -m src.catalog` validates:

- one catalog asset for every manifest file;
- catalog source files against the manifest;
- unique asset and per-asset column names;
- required catalog structure and documentary evidence;
- exact column coverage in both directions against RAW headers;
- relationship asset and column endpoints; and
- required relationship structure and unique relationship names.

Only the RAW header record is read. RAW content is not scanned or modified. Results are written to `data/results/catalog/`.

These checks validate metadata structure and physical alignment. They do not validate business correctness, establish keys, enforce relationships, or constitute Data Quality rules.

