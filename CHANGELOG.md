# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Step 6A metadata-driven historical snapshots from read-only TRUSTED with inclusive ISO cutoffs.
- Explicit YYMMDD century metadata, dependency resolution, and temporal/reference construction validation.
- Physical row preservation, safe snapshot rebuilds, and replay execution evidence.
- Synthetic replay tests and historical replay architecture documentation.
- Step 5B explicit RAW/TRUSTED selection for the existing DQ engine without changing rule semantics.
- `data_zone` and resolved `data_path` metadata in new DQ execution records.
- Backward-compatible zone-aware DQ observability history using nullable run columns.
- Tests for TRUSTED revalidation, input immutability, legacy history, and cross-zone coexistence.
- Step 5 policy-driven standardization engine and complete local TRUSTED publication.
- `CARD-STD-001` for strict full-match normalization of documented `card.issued` representation.
- Generic `regex_replace` policy operator, policy validation, transformation evidence, and atomic directory publication.
- Focused tests for authorization boundaries, physical preservation, failure recovery, and idempotent rebuilds.
- Step 4 local DQ observability builder and reproducible DuckDB execution history.
- `dq_runs` and `dq_rule_results` analytical tables sourced exclusively from DQ JSON evidence.
- Strict source-record validation, deterministic full rebuild, and atomic database publication.
- JSON observability build records plus focused history, idempotency, and failure-safety tests.
- Step 3 metadata-driven Data Quality engine using DuckDB against unchanged RAW files.
- Twenty evidence-linked YAML rules across validity, uniqueness, and referential-integrity dimensions.
- Generic `allowed_values`, `regex_format`, `unique`, and `reference_exists` operators with explicit empty-value policies.
- Rule metadata validation, deterministic violation samples, and JSON DQ execution results.
- Focused DQ tests and Data Quality architecture documentation.
- Step 2 source-documentation metadata catalog for eight Berka assets and their relationships.
- Local catalog validator for manifest coverage, required structure, RAW-header consistency, uniqueness, and relationship endpoints.
- JSON execution records for catalog validation runs.
- Focused catalog-validation tests and metadata catalog architecture documentation.
- Step 1B local DuckDB profiling over manifest-declared RAW files.
- Physical table and column metrics with deterministic sample values.
- JSON profiling execution results for successful and failed runs.
- Profiling tests for metrics, empty parsing, missing RAW input, read-only behavior, and zero-denominator handling.
- Local profiling architecture and metric-definition documentation.
- Step 1A local manifest-driven ingestion command.
- Source existence, SHA-256, and record-count integrity validation.
- Atomic RAW publication with preservation of exact source bytes.
- JSON execution metadata for successful and failed ingestion runs.
- Focused `unittest` coverage for success and failure behavior.
- Local ingestion architecture and operating instructions.
- Initial repository structure and data-zone placeholders.
- Dataset manifest for the PKDD'99 Czech Financial Dataset.
- Source baseline assessment documenting the supplied dataset and its verified files.
- Initial project README.
