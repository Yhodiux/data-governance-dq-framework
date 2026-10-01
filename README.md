# Data Governance & Data Quality Framework

Metadata-driven Data Governance and Data Quality framework for profiling, cataloging, validating, scoring and tracing relational banking data.

## Purpose

This portfolio project will build a small, executable framework that demonstrates practical Data Governance and Data Quality capabilities over relational banking data. It is not merely a theoretical governance exercise: planned governance metadata will be connected incrementally to executable data processing, validation, measurement, and traceability capabilities.

## Initial dataset

The initial dataset is the PKDD'99 Czech Financial Dataset (Berka Dataset), a relational banking dataset composed of eight source files covering accounts, clients, dispositions, permanent orders, transactions, loans, credit cards, and district demographic data.

The original source files are immutable project inputs. They are kept separate from RAW data, which is produced by the controlled local ingestion process rather than by manually copying source files.

## Local ingestion

Step 1A provides a manifest-driven local ingestion command. It verifies the existence, SHA-256 checksum, and physical record count of every expected source file before publishing byte-identical copies to `data/raw/`. These are source-integrity checks, not business Data Quality rules.

Install the only runtime dependency:

```bash
python -m pip install -r requirements.txt
```

Run ingestion from the repository root:

```bash
python -m src.ingestion --source berka-source/berka-dataset
```

Each run writes a JSON execution record to `data/results/ingestion/`. A failed validation returns a non-zero exit code and leaves the existing RAW dataset unchanged.

## Local profiling

Step 1B profiles the physical textual representation of every manifest-declared RAW file with DuckDB. It reports table dimensions and per-column empty, non-empty, distinct, uniqueness, lexical range, and deterministic sample measurements without converting dates, numbers, or business codes.

After a successful ingestion, run from the repository root:

```bash
python -m src.profiling
```

Use `python -m src.profiling --raw <path>` only when profiling another controlled RAW location. Each run writes a JSON result to `data/results/profiling/`. Profiling measurements are evidence, not automatic Data Quality findings or rules.

## Metadata catalog

Step 2 provides version-controlled YAML definitions for all manifest-declared assets, their documented columns and values, and source-supported relationships. Every definition identifies its source-documentation evidence; profiling measurements and future governance decisions remain separate.

Validate catalog structure and RAW-header consistency from the repository root:

```bash
python -m src.catalog
```

The command reads only RAW headers and writes a JSON execution record to `data/results/catalog/`. Catalog validation checks metadata consistency; it is not business Data Quality validation.

In this project:

- **SOURCE** means the original external dataset, unchanged and outside version control.
- **RAW** means exact source files published only after validation against the dataset manifest.
- **PROFILING RESULTS** are physical measurements produced from RAW.
- **SOURCE METADATA CATALOG** contains definitions supported by source documentation.
- **GOVERNANCE DECISIONS**, **TRUSTED**, and business **DATA QUALITY** capabilities are not implemented yet.

## Planned capabilities

The project is expected to add these capabilities incrementally:

- metadata-driven data profiling;
- data cataloging and governance metadata;
- configurable Data Quality validation;
- Data Quality scoring;
- lineage and traceability; and
- reporting or dashboard views over results.

These capabilities are planned and have not yet been implemented.

## Current status

**Step 2 - Metadata Catalog.** The source baseline, local ingestion, physical RAW profiling, source-documentation catalog, and catalog validator are implemented. Business Data Quality execution, scoring, lineage processing, governance decisions, trusted-layer transformations, dashboards, and cloud components have not been implemented.

See [`docs/source/dataset_assessment.md`](docs/source/dataset_assessment.md) for the source baseline, [`config/dataset_manifest.yaml`](config/dataset_manifest.yaml) for the expected files, [`docs/architecture/local_ingestion.md`](docs/architecture/local_ingestion.md) for ingestion, [`docs/architecture/local_profiling.md`](docs/architecture/local_profiling.md) for profiling, and [`docs/architecture/metadata_catalog.md`](docs/architecture/metadata_catalog.md) for catalog structure and boundaries.
