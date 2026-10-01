# Data Quality metrics (Step 8)

Run `python -m src.metrics` from the repository root. The only input is the
read-only `data/results/observability/dq_history.duckdb`, specifically `dq_runs`
and `dq_rule_results`. The output is `data/results/metrics/dq_metrics.duckdb`.
No rules are recalculated, no datasets/metadata are read, and traceability is
not combined with observability: those projections contain the same evaluations.

## Evaluation metrics

`dq_evaluation_metrics` has primary key `(run_id, rule_id)` and these columns:

```text
run_id, rule_id: VARCHAR NOT NULL
started_at: TIMESTAMP NOT NULL
data_zone: VARCHAR NULL
snapshot_cutoff: DATE NULL
asset, column_name, dimension, expectation_type, empty_policy, status: VARCHAR NOT NULL
rows_total, rows_evaluated, rows_not_evaluated: BIGINT NOT NULL
evaluation_ratio: DOUBLE NULL
violations, conforming_evaluations: BIGINT NOT NULL
violation_ratio, compliance_ratio: DOUBLE NULL
```

For recorded T (total), E (evaluated), and V (violations), the derived fields are
T-E, E/T, E-V, and V/E. Zero denominators yield NULL. `rows_not_evaluated` means
outside this evaluation, not incompleteness. Recorded compliance is retained and
validated against (E-V)/E with absolute tolerance 1e-12 when E>0; for E=0 it must
be NULL. Rule status is retained, not recalculated or confused with run status.
Dimensions, operators, and empty policies are copied from recorded results;
absent dimensions are never invented.

## Aggregate metrics

`dq_aggregate_metrics` contains:

```text
aggregation_level: VARCHAR NOT NULL
run_id, data_zone: VARCHAR NULL
snapshot_cutoff: DATE NULL
asset, dimension: VARCHAR NULL
evaluations, distinct_rules, passed_evaluations, failed_evaluations: BIGINT NOT NULL
pass_ratio: DOUBLE NULL
row_rule_evaluations, row_rule_violations, row_rule_conforming: HUGEINT NOT NULL
weighted_violation_ratio, weighted_compliance_ratio: DOUBLE NULL
```

HUGEINT preserves sums that can exceed a single BIGINT evaluation count.
The six levels are RUN, RUN_ASSET, RUN_DIMENSION, SNAPSHOT, SNAPSHOT_ASSET, and
SNAPSHOT_DIMENSION. RUN levels retain run ID and its recorded zone/cutoff.
SNAPSHOT levels have run_id=NULL and require zone `snapshot` plus a non-NULL
cutoff. Each cutoff is grouped independently; no aggregate pools distinct cutoffs.
If several runs share a cutoff, snapshot aggregates count their recorded
evaluations repeatedly, while RUN aggregates keep them separate. They do not
deduplicate a business population. Unused asset/dimension keys are NULL.

`evaluations` counts rule evaluations; `distinct_rules` counts rule IDs.
PASSED/FAILED evaluation counts produce `pass_ratio=passed/evaluations`.
The row-rule counts sum E, V, and E-V; their weighted ratios divide summed
violations or conforming evaluations by summed E. A zero denominator yields NULL.

These are **row-rule evaluations**, not unique rows, defective records, a fraction
of clean rows, or asset-wide quality. The same row may participate in multiple
rules. `pass_ratio` weights each rule evaluation equally; weighted compliance
uses evaluated row-rule counts. Neither is an arbitrary global DQ score.
The included rule set and denominators must remain visible when consuming them.

## Historical identity and interpretation

Legacy zone and cutoff remain NULL. The source legacy `data_path` is also NULL;
this output does not add a path field or reconstruct that identity. Legacy results
appear only in evaluation metrics and RUN levels, never SNAPSHOT levels. No
RAW_VS_TRUSTED view or official comparison is created. Legacy and TRUSTED currently
share 20 rule IDs/scopes and may be inspected descriptively; this does not prove
the legacy is RAW, dataset version identity, or causal lineage.

Snapshot differences retain populations, violations, ratios, and rule status.
Changes in population mean they are descriptive differences, not automatically
improvement/degradation or classified trends. Cutoffs and dimensions are never
hard-coded. The recorded rule ID is `CAR-VAL-002`, not `CARD-VAL-002`.

## Validation and deterministic publication

Missing required fields, duplicate evaluations, unknown runs, unsupported rule
status, invalid/negative counts, E>T, V>E, or out-of-range/inconsistent compliance
fail closed. NULL denominators remain NULL. No new thresholds or business rules
are introduced. Empty source evaluations produce empty analytical tables.

Inputs and inserts use stable ordering; queries should specify ORDER BY.
Full rebuild uses a sibling staging DB, SQL constraints, CHECKPOINT and close,
then `os.replace`. Failure before/during replacement preserves the previous DB.
Post-publication residue cleanup is best-effort and cannot report a false FAILED.
There are no build UUIDs/timestamps in analytical tables. Concurrent builders
are not supported because the staging name is fixed. No build record is created.

## Acceptance SQL

Bind the selected run ID for A, C and D. A real current run has 20 rules;
synthetic tests use smaller independent inputs.

```sql
-- A: rule metrics of a selected run
SELECT rule_id,asset,dimension,status,rows_total,rows_evaluated,rows_not_evaluated,
       evaluation_ratio,violations,conforming_evaluations,violation_ratio,compliance_ratio
FROM dq_evaluation_metrics WHERE run_id=? ORDER BY rule_id;

-- B: failed rules by asset and execution
SELECT run_id,asset,rule_id,rows_evaluated,violations,compliance_ratio
FROM dq_evaluation_metrics WHERE status='FAILED' ORDER BY run_id,asset,rule_id;

-- C: dimension metrics within one run
SELECT dimension,evaluations,passed_evaluations,failed_evaluations,pass_ratio,
       row_rule_evaluations,row_rule_violations,weighted_compliance_ratio
FROM dq_aggregate_metrics WHERE aggregation_level='RUN_DIMENSION' AND run_id=? ORDER BY dimension;

-- D: asset metrics within one run
SELECT asset,evaluations,passed_evaluations,failed_evaluations,pass_ratio,
       row_rule_evaluations,row_rule_violations,weighted_compliance_ratio
FROM dq_aggregate_metrics WHERE aggregation_level='RUN_ASSET' AND run_id=? ORDER BY asset;

-- E: cutoff summaries with explicit denominators
SELECT snapshot_cutoff,evaluations,distinct_rules,row_rule_evaluations,row_rule_violations,
       weighted_violation_ratio,weighted_compliance_ratio
FROM dq_aggregate_metrics WHERE aggregation_level='SNAPSHOT' ORDER BY snapshot_cutoff;

-- F: descriptive observations of one rule across snapshots, without classification
SELECT run_id,snapshot_cutoff,rule_id,status,rows_evaluated,violations,compliance_ratio
FROM dq_evaluation_metrics WHERE data_zone='snapshot' AND rule_id='CAR-VAL-002'
ORDER BY snapshot_cutoff,started_at,run_id;

-- G: legacy identity remains unknown
SELECT DISTINCT run_id,data_zone,snapshot_cutoff
FROM dq_evaluation_metrics WHERE data_zone IS NULL ORDER BY run_id;

-- H: recorded legacy/TRUSTED results with NULL not relabelled RAW
SELECT run_id,data_zone,rule_id,status,rows_evaluated,violations,compliance_ratio
FROM dq_evaluation_metrics WHERE rule_id='CAR-VAL-002' AND (data_zone IS NULL OR data_zone='trusted')
ORDER BY started_at,run_id;
```
