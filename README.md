# Data Governance & Data Quality Framework

Metadata-driven Data Governance and Data Quality framework for profiling, cataloging, validating, scoring and tracing relational banking data.

## Purpose

This portfolio project will build a small, executable framework that demonstrates practical Data Governance and Data Quality capabilities over relational banking data. It is not merely a theoretical governance exercise: planned governance metadata will be connected incrementally to executable data processing, validation, measurement, and traceability capabilities.

## Initial dataset

The initial dataset is the PKDD'99 Czech Financial Dataset (Berka Dataset), a relational banking dataset composed of eight source files covering accounts, clients, dispositions, permanent orders, transactions, loans, credit cards, and district demographic data.

The original source files are immutable project inputs. They are kept separate from future RAW data, which must be produced by an ingestion process rather than by manually copying source files.

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

**Source Baseline.** The repository structure, dataset manifest, and source assessment are initialized. No ingestion, profiling, Data Quality execution, scoring, lineage processing, dashboards, or cloud components have been implemented.

See [`docs/source/dataset_assessment.md`](docs/source/dataset_assessment.md) for the source baseline and [`config/dataset_manifest.yaml`](config/dataset_manifest.yaml) for the expected files, record counts, and checksums.

