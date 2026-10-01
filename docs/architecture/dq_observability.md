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

`dq_runs` stores one row per DQ execution: identifiers and timestamps normalized to UTC, elapsed time, technical status, rule counts, execution-error count, source JSON filename, and nullable `data_zone`/`data_path` execution metadata. Step 6B adds nullable `snapshot_cutoff DATE` without changing `dq_rule_results`.

New DQ records preserve their explicit `raw`, `trusted`, or `snapshot` zone and resolved physical path. Snapshot identity is projected from a validated ISO cutoff into `snapshot_cutoff DATE`; RAW/TRUSTED and legacy records retain SQL `NULL`. Historical records created before zone metadata remain valid and are loaded with SQL `NULL` for zone and path too. The builder does not infer that a legacy run was RAW merely because RAW used to be the default, or infer a cutoff from a directory name. Historical JSON is never rewritten.

`dq_rule_results` stores the factual rule results contained by each execution: rule identity and scope, operator metadata, status, row counts, violations, compliance ratio, deterministic sample violations as JSON, and source filename. `(run_id, rule_id)` is the primary key.

Technically failed DQ executions are retained even when they have no rule results. A DQ rule status of `FAILED` is preserved and does not make the observability build fail.

No views are created because the two small normalized tables already support the required factual queries directly.

## Deterministic full rebuild

Every invocation discovers all `*.json` files in deterministic filename order, validates the complete set, and rebuilds both tables from scratch. It never appends to the existing database. Repeating a build over unchanged inputs therefore produces the same logical history without duplicate rows or hidden incremental state.

## Input validation and malformed records

The builder validates required run and rule-result fields, usable value types, ISO-8601 timestamps, unique run IDs, unique rule IDs within each run, and sample representation. Optional zone metadata must either be absent as a complete legacy pair or contain both a supported zone and non-empty path. For technically successful DQ runs, declared totals and passed/failed counts must agree with contained rule results.

Consistency rules that assume complete execution are not imposed on technically failed DQ runs; their available evidence is preserved without inventing missing results.

Snapshot runs require a strictly formatted real ISO cutoff. A null or absent
cutoff is accepted only for a `FAILED` snapshot execution before evaluation:
zero declared/passed/failed rules, no rule results, and nonempty execution errors.
The same narrow exception permits a null path when the CLI lacked an explicit
snapshot input. Runs with any rule results require a valid cutoff and nonempty
path, even if technically failed. RAW/TRUSTED and legacy records accept absent
or null cutoffs; a non-null cutoff outside snapshot is rejected. Invalid non-null
dates are never accepted as projected identity.

Malformed JSON and structurally unsafe records are never skipped. One invalid input fails the entire build with its filename and reason, preventing a misleading partial history.

## Atomic publication and traceability

After all source records pass validation, the builder creates and closes a staging DuckDB file beside the final database. `os.replace` publishes that complete file. A validation or staging failure leaves an existing published database unchanged and removes staging residue.

Each builder invocation also writes a JSON execution record under `data/results/observability/runs/`. This record describes the observability build, not a DQ execution, and uses a distinct `obs-` run identifier.

## Interpretation boundaries

The history records executions; it does not create a global DQ score, weights, thresholds, or traffic-light rating. Repeated identical results over static RAW do not demonstrate improvement or deterioration. They only demonstrate that separate executions recorded the same facts.

Violations and compliance ratios across snapshots do not automatically indicate
improvement or deterioration because the evaluated population changes. Step 6B
preserves declared cutoff metadata in execution history; it does not interpret
trends or implement temporal analytical views. Temporal observability, incremental
ingestion, source-version tracking, dashboards, remediation, and cloud publication
remain outside this implementation.
