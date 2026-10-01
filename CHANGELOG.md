# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

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
