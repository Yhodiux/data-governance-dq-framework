# Metadata-Driven Data Quality

## Purpose and boundaries

The DQ engine evaluates explicit Data Quality expectations declared in version-controlled YAML. Python orchestrates execution and DuckDB evaluates the rules against an explicitly selected, unchanged data zone.

Rules are metadata-driven so that expectations, scope, empty-value behavior, and evidence remain reviewable outside the engine. The Python implementation contains generic operators only; it does not contain Berka asset names or dataset-specific validation branches.

RAW remains the default for backward-compatible execution. TRUSTED revalidation is explicit:

```bash
python -m src.dq
python -m src.dq --data-path data/trusted --data-zone trusted
```

`data_path` selects the physical input and `data_zone` records its semantic label (`raw` or `trusted`). The engine does not infer the label from a directory name. Both zones use exactly the same rule files, metadata validation, operators, empty policies, and PASS/FAIL semantics; there is no TRUSTED-specific rule branch.

Profiling and Data Quality remain distinct:

- profiling measures what is physically present;
- a DQ rule states an explicit expectation supported by catalog or source evidence;
- a DQ result records what happened when that expectation was evaluated.

A profiling result alone does not create a rule.

## Rule schema and provenance

Each rule declares:

- `id`, `asset`, and `column`;
- one of the implemented `dimension` values;
- an `expectation` with a generic operator and its arguments;
- an explicit `empty_policy`; and
- `evidence` linking to a catalog column, catalog relationship, or documented source constraint.

Allowed-value rules repeat executable values in rule YAML for clarity. Metadata validation requires the set to equal the catalog column's `documented_values` keys, preventing silent drift. Regex rules reference a catalog `documented_format`; the regex is an executable representation, not the underlying evidence. Referential rules must match the endpoints of their named catalog relationship.

## Implemented dimensions and operators

Only these dimensions are implemented:

- `validity`: `allowed_values` and `regex_format`;
- `uniqueness`: `unique`;
- `referential_integrity`: `reference_exists`.

Operator semantics:

- `allowed_values` counts evaluated rows whose exact physical text is outside the declared set.
- `regex_format` applies DuckDB full-value matching without trimming, parsing, or normalization.
- `unique` groups evaluated physical values and counts every row belonging to a group with more than one occurrence. Samples contain the duplicated values.
- `reference_exists` counts source rows without an exact textual match in the declared target column. It is a DQ check, not a database foreign-key constraint.

For all operators, samples contain at most five distinct violation values in deterministic lexical order.

## Empty-value policy

Every rule declares one of:

- `ignore`: zero-length physical strings are excluded from `rows_evaluated`;
- `evaluate`: zero-length physical strings are evaluated by the operator.

Empty strings are not silently reclassified as business NULL. Completeness is not implemented because no completeness expectations have been established for this phase. In particular, the engine does not create completeness defects for observed empty values.

## Results and pass/fail behavior

Rules use a strict zero-violation policy:

- `violations == 0`: `PASSED`;
- `violations > 0`: `FAILED`.

No tolerances, warning level, weighting, or global score is applied. `compliance_ratio` is `(rows_evaluated - violations) / rows_evaluated`, or JSON `null` when no rows are evaluated.

Execution status is separate from rule status. A run is `SUCCESS` when valid rules execute technically, even when one or more rules return `FAILED`. Invalid metadata, missing data-zone files, parser failures, or operator failures produce execution status `FAILED` and a non-zero CLI exit code.

New execution records include the explicit `data_zone` and resolved `data_path`. Historical records are never rewritten. Standardization and DQ revalidation remain separate commands: DQ reads its selected input without transforming it, and standardization does not invoke DQ automatically.

## Documented versus observed `card.issued`

The catalog records the documented `YYMMDD` format. The physical baseline observes a time suffix in RAW. The rule executes the strict `^\d{6}$` full-value pattern without changing RAW. In the initial real execution, all 892 evaluated values violated that physical-format expectation because values such as `931107 00:00:00` include the observed suffix. This evidence is preserved without adding a card-specific engine branch or changing the rule to fit the data.

## Current limitations

This phase does not implement completeness, semantic calendar validation, thresholds, scoring, remediation, governance assignments, TRUSTED output, dashboards, or cloud execution. Referential checks compare physical text and do not assert enforced database constraints.
