# Execution traceability (Step 7B)

Run from the repository root:

```bash
python -m src.traceability
```

The output is `data/results/traceability/execution_traceability.duckdb`.
Step 7A structural lineage describes what the framework declares/supports.
Step 7B execution traceability describes what a recorded execution reports
actually happened. Current execution records do not contain upstream run
identifiers. Therefore Step 7B does not claim causal run-to-run lineage.

## Inputs and evidence

The builder reads only `*.json` directly under `data/results/ingestion/`,
`data/results/dq/`, `data/results/standardization/`, and `data/results/replay/`.
It never reads datasets, independent metadata, profiling, observability or
lineage databases, or invokes pipelines. Original JSON records remain unchanged.
`execution_record_path` is a relative POSIX path to the authoritative original
record; each metric and detail references its owning run through a foreign key.
Whole records are not copied into the database. Replay's embedded metadata and
relationships stay in the original JSON rather than becoming execution edges.

## Schema and mappings

There are exactly three tables:

```text
execution_runs
  run_id VARCHAR PRIMARY KEY
  process_type VARCHAR NOT NULL
  status VARCHAR NOT NULL
  started_at TIMESTAMP NOT NULL
  completed_at TIMESTAMP NOT NULL
  elapsed_seconds DOUBLE NULL
  data_zone VARCHAR NULL
  snapshot_cutoff DATE NULL
  source_path VARCHAR NULL
  output_path VARCHAR NULL
  execution_record_path VARCHAR NOT NULL

execution_metrics
  run_id VARCHAR NOT NULL REFERENCES execution_runs(run_id)
  metric_name VARCHAR NOT NULL
  metric_value DOUBLE NOT NULL
  PRIMARY KEY(run_id, metric_name)

execution_details
  detail_id VARCHAR PRIMARY KEY
  run_id VARCHAR NOT NULL REFERENCES execution_runs(run_id)
  detail_type VARCHAR NOT NULL
  semantic_id VARCHAR NULL
  asset VARCHAR NULL
  column_name VARCHAR NULL
  status VARCHAR NULL
  rows_total BIGINT NULL
  rows_evaluated BIGINT NULL
  rows_changed BIGINT NULL
  violations BIGINT NULL
  compliance_ratio DOUBLE NULL
  input_rows BIGINT NULL
  output_rows BIGINT NULL
  attributes JSON NOT NULL
```

Database CHECK constraints restrict process/detail types and require completion
not to precede start. Input timestamps require timezone information and are
normalized to UTC before storage in the TIMESTAMP columns. Use the explicit
timestamps, not the timestamp-shaped portion of a run ID.

| Process | source_path | output_path | data_zone | snapshot_cutoff |
| --- | --- | --- | --- | --- |
| ingestion | `source_path` | `raw_path` | NULL | NULL |
| dq | `data_path` if recorded | NULL | `data_zone` if recorded | `snapshot_cutoff` if recorded |
| standardization | NULL | `trusted_path` | NULL | NULL |
| replay | `source_path` | `snapshot_path` | `source_zone` | `cutoff` |

The DQ legacy record keeps unknown path, zone and cutoff as NULL. Standardization
does not record its input path, so it is not reconstructed from the current RAW
default. Replay's `data_zone` labels its recorded source, not a DQ evaluation.
Absent optional values stay NULL; no missing duration or metric is computed.

Metrics are projected only when their explicit scalar fields exist:

- ingestion: `files_expected`, `files_processed`;
- dq: `rules_total`, `rules_passed`, `rules_failed`;
- standardization: `policies_total`, `policies_applied`, `assets_total`,
  `assets_copied`, `assets_transformed`;
- replay: no run-level aggregate metrics.

Nested details are scoped to their own execution:

| Type | Semantic identity | Dedicated columns | Attributes retained when present |
| --- | --- | --- | --- |
| INGESTION_FILE | `file_name` | `validation_status` as status | expected/actual SHA-256 and record counts |
| DQ_RULE | `rule_id` | asset, column, rule status, row counts, violations, compliance ratio | dimension, expectation type, empty policy, sample violations |
| STANDARDIZATION_POLICY | `policy_id` | asset, column, total/evaluated/changed rows | transformation type, unchanged rows, sample changes |
| REPLAY_ASSET | `asset` | asset, input/output rows | strategy |

Expected ingestion values are expectations, not observations. Rule status is
separate from engine execution status. A standardization detail can report actual
changed rows, while the corresponding Step 7A structural policy only describes
authorization. Details do not establish row identity or upstream provenance.

## Validation, determinism and publication

Malformed JSON (including duplicate keys), missing/duplicate run IDs, mismatched
filenames, incompatible structures, unknown/conflicting process types, missing
status, invalid timestamps/order, duplicate details/metrics, invalid numeric
values and foreign-key violations fail closed. Required detail arrays may be
empty for failed pre-evaluation runs. Optional historical paths, zones, cutoffs,
duration and absent metrics are not invented. No completeness assumptions about
the entire execution history are made.

Inputs and rows are sorted. Detail IDs are `detail:` plus SHA-256 of canonical
JSON `[process_type, run_id, detail_type, semantic_id]`, independent of array
position. Attributes use sorted-key canonical JSON. Main tables contain no build
UUIDs/timestamps; identical inputs produce identical logical rows.

A full rebuild constructs the sibling `.execution_traceability.duckdb.staging`,
enforces constraints, verifies counts, checkpoints, closes, and publishes with
`os.replace`. Failure before/during replacement preserves the previous database.
Residual cleanup after successful replacement is best-effort and does not turn
SUCCESS into FAILED. Concurrent builders are not supported; the staging name is
fixed. No execution record is produced for this builder.

Equal paths, cutoffs, timestamps, or content do not identify a producer run.
Two replay runs publishing the same snapshot location remain two executions.
No upstream/downstream/parent identifiers or causal execution edges are created.

## Acceptance SQL

Open the database read-only. Query C takes a run ID parameter.

```sql
-- A: chronological executions
SELECT run_id, process_type, status, started_at, completed_at
FROM execution_runs ORDER BY started_at, run_id;

-- B: DQ zones and declared cutoffs
SELECT run_id, data_zone, snapshot_cutoff, source_path
FROM execution_runs WHERE process_type='dq' ORDER BY started_at, run_id;

-- C: failed rules in a specific DQ execution (bind its run_id)
SELECT semantic_id AS rule_id, asset, column_name, violations, compliance_ratio
FROM execution_details
WHERE detail_type='DQ_RULE' AND status='FAILED' AND run_id=?
ORDER BY semantic_id;

-- D: policies reporting changed rows
SELECT run_id, semantic_id AS policy_id, asset, column_name, rows_changed
FROM execution_details
WHERE detail_type='STANDARDIZATION_POLICY' AND rows_changed > 0
ORDER BY run_id, semantic_id;

-- E: replay input/output rows, retaining execution identity
SELECT r.run_id, r.snapshot_cutoff, d.asset, d.input_rows, d.output_rows
FROM execution_runs r JOIN execution_details d USING(run_id)
WHERE d.detail_type='REPLAY_ASSET'
ORDER BY r.started_at, r.run_id, d.asset;

-- F: both 1998 replay executions, without collapsing a shared path/cutoff
SELECT run_id, snapshot_cutoff, output_path
FROM execution_runs WHERE process_type='replay' AND snapshot_cutoff=DATE '1998-12-31'
ORDER BY started_at, run_id;

-- G: DQ executions retaining unknown legacy identity
SELECT run_id, data_zone, source_path, snapshot_cutoff, execution_record_path
FROM execution_runs WHERE process_type='dq' AND data_zone IS NULL
ORDER BY started_at, run_id;
```
