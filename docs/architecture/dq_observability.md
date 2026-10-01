# Local DQ Observability and Run History

## Purpose and data flow

Step 4 turns individual Data Quality execution records into a queryable local history:

```text
data/results/dq/*.json
            |
            v
observability history builder
            |
            v
data/results/observability/dq_history.duckdb
            |-- dq_runs
            `-- dq_rule_results
```

The DQ JSON files remain the immutable source evidence. DuckDB is a derived analytical projection that can be reproduced from those records. The builder does not read RAW, rerun rules, recalculate violations, or reinterpret statuses.

## Analytical tables

`dq_runs` stores one row per DQ execution: identifiers and timestamps normalized to UTC, elapsed time, technical status, rule counts, execution-error count, source JSON filename, and nullable `data_zone`/`data_path` execution metadata.

New DQ records preserve their explicit `raw` or `trusted` zone and resolved physical path. Historical records created before zone metadata remain valid and are loaded with SQL `NULL` for both columns. The builder does not infer that a legacy run was RAW merely because RAW used to be the default.

`dq_rule_results` stores the factual rule results contained by each execution: rule identity and scope, operator metadata, status, row counts, violations, compliance ratio, deterministic sample violations as JSON, and source filename. `(run_id, rule_id)` is the primary key.

Technically failed DQ executions are retained even when they have no rule results. A DQ rule status of `FAILED` is preserved and does not make the observability build fail.

No views are created because the two small normalized tables already support the required factual queries directly.

## Deterministic full rebuild

Every invocation discovers all `*.json` files in deterministic filename order, validates the complete set, and rebuilds both tables from scratch. It never appends to the existing database. Repeating a build over unchanged inputs therefore produces the same logical history without duplicate rows or hidden incremental state.

## Input validation and malformed records

The builder validates required run and rule-result fields, usable value types, ISO-8601 timestamps, unique run IDs, unique rule IDs within each run, and sample representation. Optional zone metadata must either be absent as a complete legacy pair or contain both a supported zone and non-empty path. For technically successful DQ runs, declared totals and passed/failed counts must agree with contained rule results.

Consistency rules that assume complete execution are not imposed on technically failed DQ runs; their available evidence is preserved without inventing missing results.

Malformed JSON and structurally unsafe records are never skipped. One invalid input fails the entire build with its filename and reason, preventing a misleading partial history.

## Atomic publication and traceability

After all source records pass validation, the builder creates and closes a staging DuckDB file beside the final database. `os.replace` publishes that complete file. A validation or staging failure leaves an existing published database unchanged and removes staging residue.

Each builder invocation also writes a JSON execution record under `data/results/observability/runs/`. This record describes the observability build, not a DQ execution, and uses a distinct `obs-` run identifier.

## Interpretation boundaries

The history records executions; it does not create a global DQ score, weights, thresholds, or traffic-light rating. Repeated identical results over static RAW do not demonstrate improvement or deterioration. They only demonstrate that separate executions recorded the same facts.

Temporal replay, incremental ingestion, source-version tracking, dashboards, remediation, and cloud publication remain possible future work and are not implemented.
