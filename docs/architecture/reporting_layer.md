# Step 11A — Power BI reporting layer

Run `python -m src.reporting` from the repository root. The builder reads only
the existing Metrics, Governance, Execution Traceability and Structural Lineage
DuckDB databases with READ_ONLY attachments. It publishes exactly eight Parquet
files in `data/results/reporting/`, without new dependencies, a reporting DuckDB,
metadata reads, dataset reads, or execution of original pipelines.

## Datasets and sources

Source paths below are relative to `data/results/`.

| File | Source | Grain / key |
| --- | --- | --- |
| assets.parquet | lineage/lineage.duckdb: lineage_nodes, node_type=asset AND zone=source | asset PK; one catalog asset, not one per zone |
| rules.parquet | lineage/lineage.duckdb: lineage_nodes, node_type=dq_rule | semantic_id AS rule_id PK |
| dq_evaluations.parquet | metrics/dq_metrics.duckdb: dq_evaluation_metrics | (run_id, rule_id) PK |
| dq_summaries.parquet | metrics/dq_metrics.duckdb: dq_aggregate_metrics, levels RUN, RUN_ASSET, RUN_DIMENSION only | (aggregation_level, run_id, asset, dimension); summary_id technical PK |
| governance_issues.parquet | governance/governance_registry.duckdb: governance_issues | issue_id PK |
| governance_decisions.parquet | governance/governance_registry.duckdb: governance_decisions, LEFT JOIN lineage_nodes on policy_id=semantic_id AND node_type=standardization_policy | decision_id PK |
| execution_runs.parquet | traceability/execution_traceability.duckdb: execution_runs | run_id PK |
| execution_details.parquet | traceability/execution_traceability.duckdb: execution_details | detail_id PK |

No SNAPSHOT/SNAPSHOT_* aggregate levels or temporal views are exported. Each
run-level summary still carries data_zone and snapshot_cutoff. The technical
summary ID is SHA-256 of a JSON array [aggregation_level, run_id, asset, dimension],
preserving NULL identity without sentinel collisions. No business rule is added.

## Stable schemas

All unspecified types below are VARCHAR. Optional source values remain NULL;
publication does not substitute unknown zones, dates, actors or durations.

- **assets:** asset, node_id.
- **rules:** rule_id, asset, column_name, node_id.
- **dq_evaluations:** run_id, rule_id, started_at TIMESTAMP, data_zone,
  snapshot_cutoff DATE, asset, column_name, dimension, expectation_type,
  empty_policy, status, rows_total BIGINT, rows_evaluated BIGINT,
  rows_not_evaluated BIGINT, evaluation_ratio DOUBLE, violations BIGINT,
  conforming_evaluations BIGINT, violation_ratio DOUBLE, compliance_ratio DOUBLE.
- **dq_summaries:** summary_id, aggregation_level, run_id, data_zone,
  snapshot_cutoff DATE, asset, dimension, evaluations BIGINT, distinct_rules BIGINT,
  passed_evaluations BIGINT, failed_evaluations BIGINT, pass_ratio DOUBLE,
  row_rule_evaluations DECIMAL(38,0), row_rule_violations DECIMAL(38,0),
  row_rule_conforming DECIMAL(38,0), weighted_violation_ratio DOUBLE,
  weighted_compliance_ratio DOUBLE.
- **governance_issues:** issue_id, asset, column_name, rule_id, status, description.
- **governance_decisions:** decision_id, issue_id, decision_type, status,
  policy_id, decision_date DATE, actor, rationale, policy_asset, policy_column.
- **execution_runs:** run_id, process_type, status, started_at TIMESTAMP,
  completed_at TIMESTAMP, elapsed_seconds DOUBLE, data_zone, snapshot_cutoff DATE,
  source_path, output_path, execution_record_path.
- **execution_details:** detail_id, run_id, detail_type, semantic_id, asset,
  column_name, status, rows_total BIGINT, rows_evaluated BIGINT, rows_changed BIGINT,
  violations BIGINT, compliance_ratio DOUBLE, input_rows BIGINT, output_rows BIGINT,
  attributes (VARCHAR text JSON, without expansion).

Source HUGEINT row-rule counts are encoded as exact DECIMAL(38,0), because
Parquet has no DuckDB HUGEINT logical type. Cast overflow fails closed; export
roundtrip checks every value, including NULLs, counts, ratios and JSON text.
Power BI's Whole Number is suitable for current counts; counts exceeding its
integer range must not be silently converted to floating point.

## Recommended Power BI model

Import each file as one table with its explicit schema. Use these relationships:

| One side | Many side | Key |
| --- | --- | --- |
| Assets | Rules | asset |
| Assets | DQ Evaluations | asset |
| Rules | DQ Evaluations | rule_id |
| Assets | Governance Issues | asset |
| Rules | Governance Issues | rule_id |
| Governance Issues | Governance Decisions | issue_id |
| Execution Runs | DQ Evaluations | run_id |
| Execution Runs | DQ Summaries | run_id |
| Execution Runs | Execution Details | run_id |

Use unidirectional filtering from the one side. Assets→Rules→Evaluations and
Assets→Evaluations form competing paths; the same applies to Issues. Keep
Assets→Rules inactive by default and the direct Assets/Rules→facts active.
Activate the inactive relationship only in an explicit inventory measure when
needed. Avoid bidirectional fact-to-fact filtering. Summary asset relationships
are optional and must not introduce ambiguous active paths. The builder validates
nullable asset references for summaries and details, as well as the listed FKs,
rule scope, policy scope and DQ run identity across source databases.

Do not relate different runs through equal paths, timestamps or cutoffs. The
policy enrichment describes declared structural scope, not execution causality.
Current Rules is a declared inventory, not a historical version dimension.
Recorded dimension/operator/empty-policy context stays in DQ Evaluations.

## Exactly three dashboard pages

1. **Governance Overview:** Assets and Rules inventories; Issues by status;
   Decisions, rationale and associated policy. Count catalog assets separately
   from assets with DQ rules. Unknown actor/date stay blank.
2. **Data Quality:** Evaluations sliced by asset, rule, dimension and status,
   showing rows_evaluated and violations alongside compliance. Summaries provide
   complete-rule-set totals per run/asset/dimension with an explicit level filter.
3. **Historical & Traceability:** filter DQ datasets to data_zone=snapshot;
   retain snapshot_cutoff and run_id for rule/asset/dimension series. Runs and
   Details show independent execution facts, process status and evidence paths.

Counts and per-evaluation ratios are already materialized. Power BI may calculate
inventory/status counts and ratios of summed conforming_evaluations to summed
rows_evaluated for explicit selections, with zero denominator returning BLANK.
Label those aggregates as weighted row-rule compliance, never a global DQ score.
Never average compliance ratios. Summaries describe their complete source group
and do not respond semantically to arbitrary rule selections; use Evaluations for
those selections. Never sum RUN, RUN_ASSET and RUN_DIMENSION together.

Row-rule evaluations/violations count row/expectation combinations, not unique
bad rows. Execution Details repeats some DQ evidence for traceability; do not add
its counts to DQ Evaluations. Preserve all runs, without latest/canonical
selection. Historical differences are descriptive, without improvement/degradation
labels or causal interpretations. started_at is execution time; snapshot_cutoff
is evaluated historical identity. Power BI consumes results, not DQ logic.

## Publication and verification

Rows are sorted by stable keys. The builder rejects source schema drift,
duplicate grains, NULL primary keys, invalid references and inconsistent scope
or run identity. It verifies all eight Parquet schemas and a bidirectional
EXCEPT ALL roundtrip before publishing. Determinism means identical typed logical
content, not a promise of byte-identical Parquet encoding.

Publication prepares a unique sibling staging directory, moves the previous
directory to a unique backup, then renames staging to reporting. Each rename is
atomic on the same filesystem. A failed replacement restores the previous whole
directory; a rollback failure retains its backup for recovery. Cleanup after
successful publication is best-effort and cannot turn SUCCESS into FAILED.
All recursive cleanup targets are checked against the publication parent.

Replacing a nonempty directory requires two renames on this platform: readers
can briefly see the reporting path absent. This is failure-safe whole-generation
publication, not a simultaneous snapshot guarantee for concurrent readers of
eight files. Run one builder at a time and refresh Power BI after SUCCESS;
do not refresh during publication. No file is overwritten individually.

Parquet preserves typed dates, timestamps, integer/decimal counts and NULLs;
JSON attributes are explicitly text. The layer creates no additional outputs.
Tests use synthetic source DuckDBs, cover schemas, keys, references, NULLs,
large exact counters, multiple runs, deterministic rebuilds, read-only inputs,
publication rollback and best-effort cleanup without invoking original pipelines.
