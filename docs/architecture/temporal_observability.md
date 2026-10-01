# Temporal DQ observability (Step 9)

Step 9 adds exactly four SQL views to the existing Step 8 metrics database:
`data/results/metrics/dq_metrics.duckdb`. Run `python -m src.metrics` to rebuild
its two base tables and these views together. No other database or CLI is added,
DQ is not recalculated, and no new base metrics or input sources are introduced.
The views are created in staging before the existing checkpoint/close/atomic
replacement, so a failed view creation cannot replace the prior database.

## Time and identity

`snapshot_cutoff` is the declared historical cutoff of the evaluated dataset.
`started_at` is the time the evaluation executed. They are not interchangeable:
evaluations of different historical cutoffs can execute on the same day.
Historical queries order by cutoff and run ID, not execution time. No missing
cutoffs are interpolated, and no annual cadence is assumed.

All views require `data_zone='snapshot' AND snapshot_cutoff IS NOT NULL`.
Legacy/other zones and snapshot runs without a cutoff are excluded. Every view
exposes run ID. Multiple executions of the same cutoff remain independent;
there is no latest, first, or canonical run selection and no pooling of those
runs through the SNAPSHOT aggregate levels.

## Views

| View | Source | Grain |
| --- | --- | --- |
| `dq_snapshot_rule_history` | `dq_evaluation_metrics` | run ID + rule ID + cutoff |
| `dq_snapshot_asset_history` | `dq_aggregate_metrics`, RUN_ASSET | run ID + asset + cutoff |
| `dq_snapshot_dimension_history` | `dq_aggregate_metrics`, RUN_DIMENSION | run ID + dimension + cutoff |
| `dq_snapshot_evaluation_summary` | `dq_aggregate_metrics`, RUN | run ID + cutoff |

Rule history exposes cutoff, run ID, execution start, rule ID, asset, column,
dimension, expectation type, empty policy, status, total/evaluated/not-evaluated
rows, evaluation ratio, violations, conforming evaluations, violation ratio,
and recorded compliance ratio.

The other views expose cutoff, run ID, their asset/dimension key when applicable,
evaluations, distinct rules, passed/failed evaluations, pass ratio,
`row_rule_evaluations`, `row_rule_violations`, `row_rule_conforming`,
weighted violation ratio and weighted compliance ratio. They are projections of
existing metrics, not recomputations. Nullable ratios remain NULL.

Row-rule counts mean evaluated row/expectation combinations, not unique rows,
defective records, or clean rows. Rule populations and included rule sets remain
necessary context for interpretation. Snapshot populations change, so differing
violations or ratios do not automatically imply improvement, degradation, a
trend, or causality. The views add no deltas, growth, severity, or better/worse
labels. Scope/operator/empty-policy fields in rule history remain available for
descriptive comparisons without proving immutable rule or dataset versions.

## Acceptance SQL

A-C accept a rule, asset, or dimension parameter. Consumers specify ORDER BY;
views do not promise an intrinsic row order. G exposes any repeated-cutoff
executions instead of choosing one. On current data it returns no rows; synthetic
tests supply two runs of the same cutoff and verify both in all four views.

```sql
-- A: full history of a selected rule
SELECT * FROM dq_snapshot_rule_history WHERE rule_id=?
ORDER BY snapshot_cutoff,run_id;

-- B: asset history with explicit row-rule denominator
SELECT * FROM dq_snapshot_asset_history WHERE asset=?
ORDER BY snapshot_cutoff,run_id;

-- C: dimension history
SELECT * FROM dq_snapshot_dimension_history WHERE dimension=?
ORDER BY snapshot_cutoff,run_id;

-- D: every snapshot execution summary
SELECT * FROM dq_snapshot_evaluation_summary ORDER BY snapshot_cutoff,run_id;

-- E: card date evaluations with their changing populations
SELECT snapshot_cutoff,run_id,rows_evaluated,violations,compliance_ratio,status
FROM dq_snapshot_rule_history WHERE rule_id='CAR-VAL-002'
ORDER BY snapshot_cutoff,run_id;

-- F: transaction type observations without automatic change classification
SELECT snapshot_cutoff,run_id,rows_evaluated,violations,violation_ratio,compliance_ratio,status
FROM dq_snapshot_rule_history WHERE rule_id='TRA-VAL-002'
ORDER BY snapshot_cutoff,run_id;

-- G: multiple runs of a cutoff remain distinct (also verified with synthetic runs)
SELECT snapshot_cutoff,count(*) AS runs,list(run_id ORDER BY run_id) AS run_ids
FROM dq_snapshot_evaluation_summary GROUP BY snapshot_cutoff
HAVING count(*)>1 ORDER BY snapshot_cutoff;
```
