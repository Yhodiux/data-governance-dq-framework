# Governance issues and decisions

Step 10 maintains explicit, version-controlled declarations in
`metadata/governance/issues.yaml` and `decisions.yaml`. Run
`python -m src.governance` to validate references and atomically rebuild
`data/results/governance/governance_registry.duckdb`.

DQ finding != governance decision. Governance decision != execution.
Execution evidence != causal lineage. The registry does not execute remediation,
change expectations, or connect execution runs through inferred dependencies.

## Initial declarations

| Issue | Asset/column | Rule | Status | Decision | Policy |
| --- | --- | --- | --- | --- | --- |
| GOV-ISS-001 | card.issued | CAR-VAL-002 | RESOLVED | GOV-DEC-001 / REMEDIATION_AUTHORIZED / EFFECTIVE | CARD-STD-001 |
| GOV-ISS-002 | trans.type | TRA-VAL-002 | OPEN | GOV-DEC-002 / REMEDIATION_WITHHELD / ACTIVE | NULL |
| GOV-ISS-003 | order.k_symbol | ORD-VAL-001 | OPEN | GOV-DEC-003 / REMEDIATION_WITHHELD / ACTIVE | NULL |
| GOV-ISS-004 | trans.k_symbol | TRA-VAL-004 | OPEN | GOV-DEC-004 / REMEDIATION_WITHHELD / ACTIVE | NULL |

The card finding references a legacy evaluation reporting 892 violations,
the documented YYMMDD representation and exact-suffix normalization policy,
standardization reporting 892 changed rows, and a separate TRUSTED evaluation
reporting zero violations. These are independent facts, without an upstream-run
contract. The legacy zone remains unknown. RESOLVED is a declared governance
status, not a status inferred automatically from execution evidence.

VYBER is an observed violation sample for trans.type. Whitespace is an observed
violation sample for each k_symbol rule. Existing evidence does not authorize
mapping VYBER, extending its domain, interpreting whitespace as empty/NULL, or
trimming it. REMEDIATION_WITHHELD is a new explicit Step 10 decision; absence of
a transformation policy is not evidence of a preexisting historical decision.
Actor and decision_date are NULL, without deriving them from Git or execution
timestamps. Severity, owners and business impact are not invented.

Three original generated JSON artifacts are intentionally retained in version
control so a clean checkout contains the historical execution evidence supporting
the versioned governance declarations:

- `data/results/dq/20261001T013557.424850Z-48654f22.json`
- `data/results/standardization/std-20261001T022159.952149Z-88fe0f44.json`
- `data/results/dq/20261001T023050.406156Z-b6e62a52.json`

Their contents and execution identities are preserved. Future executions produce
independent evidence and do not replace these records; no latest/canonical-run
semantics are implied. Absolute paths inside these records reflect the original
execution environment and are intentionally preserved, not used as input paths
for a new checkout. All other generated execution results remain gitignored.

## Projection and validation

`governance_issues`: issue_id VARCHAR PK, asset VARCHAR, column_name VARCHAR,
rule_id VARCHAR, status VARCHAR, description VARCHAR (all non-null).

`governance_decisions`: decision_id VARCHAR PK, issue_id VARCHAR non-null FK,
decision_type VARCHAR, status VARCHAR, policy_id VARCHAR nullable,
decision_date DATE nullable, actor VARCHAR nullable, rationale VARCHAR.
All remaining fields are non-null.

`governance_evidence`: evidence_id VARCHAR PK, parent_type VARCHAR,
parent_id VARCHAR, evidence_type VARCHAR, path VARCHAR, run_id VARCHAR nullable,
rule_id VARCHAR nullable, policy_id VARCHAR nullable, description VARCHAR.
All remaining fields are non-null. Parent types ISSUE and DECISION resolve to
their corresponding table; this polymorphic reference is validated by the builder.

Issue states are OPEN/RESOLVED; decision states ACTIVE/EFFECTIVE; types are
REMEDIATION_AUTHORIZED/REMEDIATION_WITHHELD. RESOLVED requires an EFFECTIVE
decision. Authorization requires an existing policy matching the issue scope;
withholding does not require a policy. IDs, rule/policy existence and scopes,
enums, ISO decision dates and evidence references fail closed.

Evidence types are METADATA, DQ_EXECUTION and STANDARDIZATION_EXECUTION.
Paths resolve within the project. Catalog references select the issue column;
rule and policy references select their authoritative YAML declaration. Execution
references select original JSON by filename/run_id and exactly one scoped
rule_id or policy_id. Projections in observability, metrics, traceability and
lineage are not additional execution evidence. Complete JSON results are not
copied into the registry. Reference descriptions remain authored declarations.

Evidence IDs hash parent identity, evidence type/path and optional run/rule/policy
selectors. Description edits and input ordering do not change identity. Sorted
rows yield deterministic logical rebuilds, rather than a byte-identical DuckDB
file guarantee. Publication uses a sibling staging database, checkpoint/close
and os.replace. Pre-publication failure preserves the previous database;
post-publication cleanup is best-effort. The fixed staging path assumes a single
builder at a time. No execution causal edges are created.

## Acceptance SQL

### A

```sql
SELECT issue_id, asset, column_name, rule_id, status
FROM governance_issues ORDER BY issue_id;
```

### B

```sql
SELECT issue_id, asset, column_name, status
FROM governance_issues WHERE status='OPEN' ORDER BY issue_id;
```

### C

```sql
SELECT i.issue_id, d.decision_id, d.decision_type, d.status, d.policy_id
FROM governance_issues i JOIN governance_decisions d USING(issue_id)
ORDER BY i.issue_id, d.decision_id;
```

### D

```sql
SELECT e.* FROM governance_evidence e
WHERE (e.parent_type='ISSUE' AND e.parent_id='GOV-ISS-001')
   OR (e.parent_type='DECISION' AND EXISTS
       (SELECT 1 FROM governance_decisions d
        WHERE d.decision_id=e.parent_id AND d.issue_id='GOV-ISS-001'))
ORDER BY e.parent_type, e.parent_id, e.evidence_id;
```

### E

```sql
SELECT i.issue_id, i.asset, i.column_name FROM governance_issues i
WHERE NOT EXISTS (SELECT 1 FROM governance_decisions d
  WHERE d.issue_id=i.issue_id AND d.decision_type='REMEDIATION_AUTHORIZED'
    AND d.policy_id IS NOT NULL)
ORDER BY i.issue_id;
```

### F

```sql
SELECT decision_id, issue_id, status, policy_id FROM governance_decisions
WHERE decision_type='REMEDIATION_WITHHELD' ORDER BY decision_id;
```

### G

```sql
SELECT decision_id, actor, decision_date FROM governance_decisions
ORDER BY decision_id;
```

### H

```sql
SELECT i.issue_id, i.rule_id, d.policy_id AS authorized_policy_id,
       e.evidence_type, e.path, e.run_id, e.rule_id AS evidence_rule_id,
       e.policy_id AS evidence_policy_id
FROM governance_issues i JOIN governance_decisions d USING(issue_id)
JOIN governance_evidence e ON
  (e.parent_type='ISSUE' AND e.parent_id=i.issue_id)
  OR (e.parent_type='DECISION' AND e.parent_id=d.decision_id)
WHERE i.rule_id='CAR-VAL-002' AND d.policy_id='CARD-STD-001'
ORDER BY e.evidence_id;
```

H associates references with a declaration; it does not join runs by timestamps,
paths or an inferred RAW/standardization/TRUSTED causal chain.
