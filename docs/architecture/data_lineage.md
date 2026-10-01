# Metadata-driven structural lineage (Step 7A)

Run from the repository root:

```bash
python -m src.lineage
```

The builder publishes `data/results/lineage/lineage.duckdb`. It projects the
current structural contract: an edge means that declared metadata and/or an
explicit framework contract supports that relationship. It does not assert that
a particular execution occurred. Step 7B execution traceability remains pending.

## Authoritative inputs and contract adapters

The only inputs are `config/dataset_manifest.yaml`, `metadata/catalog/*.yaml`,
`metadata/relationships.yaml`, `metadata/dq_rules/*.yaml`,
`metadata/standardization/*.yaml`, `metadata/replay/historical_snapshot.yaml`,
and explicit ingestion, standardization, replay, and DQ source contracts.
No additional lineage metadata duplicates those declarations.

The adapters in `src/lineage/core.py` encode these existing contracts:

| Component | Contract evidence | Supported correspondence |
| --- | --- | --- |
| Ingestion | `src/ingestion/core.py:publish_atomically`; `src/ingestion/__main__.py:main` | All manifest files are published to RAW; catalog `source_file` identifies each logical asset. |
| Standardization | `src/standardization/core.py:build_and_publish`; `src/standardization/__main__.py:main` | The complete manifest publication becomes TRUSTED, including unaffected assets; declared policies authorize column transformations. |
| Replay | `src/replay/core.py:run_replay`; `src/replay/__main__.py:main` | Declared replay assets are projected from TRUSTED into the structural SNAPSHOT layer. |
| DQ | `src/dq/core.py:run_dq`; `src/dq/core.py:DATA_ZONES` | The same declared rules are applicable to RAW, TRUSTED, and SNAPSHOT. |

Source files are parsed with Python AST to resolve these top-level symbols, and
the DQ zone declaration must match the adapter. Existing pure rule, policy, and
replay metadata validators are reused; no processing commands are invoked.
These are explicit adapters of reviewed contracts, not general code analysis.
A future change to contract semantics requires reviewing the corresponding
adapter even when a function retains its name.

The builder never reads RAW, TRUSTED, snapshots, DQ/replay results, observability
history, or execution records. SOURCE is a logical dataset source, not a concrete
input directory. Metadata scope and contract correspondence establish edges;
matching column names alone does not.

## Node model and schema

`lineage_nodes` has these columns:

| Column | DuckDB type | Meaning |
| --- | --- | --- |
| `node_id` | VARCHAR PRIMARY KEY | Stable typed identity. |
| `node_type` | VARCHAR NOT NULL | Exactly `asset`, `column`, `dq_rule`, or `standardization_policy`. |
| `zone` | VARCHAR nullable | `source`, `raw`, `trusted`, `snapshot`; `logical` only for relationship columns; NULL for rules/policies. |
| `asset` | VARCHAR nullable | Catalog asset, including rule/policy scope. |
| `column_name` | VARCHAR nullable | Catalog column for columns and rule/policy scope; NULL for assets. |
| `semantic_id` | VARCHAR nullable | Declared rule/policy ID; NULL for asset/column nodes. |

There are no SOURCE column nodes. Zoned column nodes exist only where needed
by DQ or explicit transformations; logical column nodes exist only for documented
relationships. Asset-level evidence does not imply column-level flow.

IDs use typed prefixes and colon-separated components, for example
`asset:source:card`, `column:raw:card:issued`, `column:logical:trans:account_id`,
`rule:CAR-VAL-002`, and `policy:CARD-STD-001`. Each component is independently
UTF-8 percent-encoded with `urllib.parse.quote(..., safe="")`. Colons and percent
characters in names therefore cannot create delimiter collisions.

## Edge model and schema

`lineage_edges` has these columns:

| Column | DuckDB type | Meaning |
| --- | --- | --- |
| `edge_id` | VARCHAR PRIMARY KEY | `edge:` plus SHA-256 of canonical JSON `[source, target, type, context]`. |
| `source_node_id` | VARCHAR NOT NULL | Foreign key to `lineage_nodes`. |
| `target_node_id` | VARCHAR NOT NULL | Foreign key to `lineage_nodes`. |
| `edge_category` | VARCHAR NOT NULL | One of the four categories below. |
| `edge_type` | VARCHAR NOT NULL | One of the six types below. |
| `context_node_id` | VARCHAR nullable | Policy foreign key, required only for `transformed_by`. |
| `evidence` | JSON NOT NULL | Nonempty array of stable evidence references. |

These are the only two database tables. Database constraints enforce node types,
primary/foreign keys, category/type combinations, and presence/absence of context.
Python validation additionally checks zones, scopes, endpoints, context typing,
and evidence resolution before staging starts.

| Category | Type | Source → target |
| --- | --- | --- |
| DATA_FLOW | `ingested_to` | SOURCE asset → RAW asset of the same catalog asset. |
| DATA_FLOW | `published_to` | RAW asset → TRUSTED asset of the same catalog asset. |
| DATA_FLOW | `replayed_to` | TRUSTED asset → SNAPSHOT asset of the same catalog asset. |
| TRANSFORMATION | `transformed_by` | RAW column → TRUSTED column, with its declared policy as context. |
| EVALUATION | `evaluated_by` | RAW/TRUSTED/SNAPSHOT column → one shared rule node. |
| DATA_RELATIONSHIP | `references` | Logical source column → logical target column. |

Every published asset uses `published_to`; it makes no claim about changed rows.
Only explicitly authorized policy columns receive `transformed_by`. For
`CARD-STD-001`, the direct edge is RAW `card.issued` → TRUSTED `card.issued`,
with `context_node_id = policy:CARD-STD-001`. There are no two-edge paths through
the policy node, nor copy edges for unaffected columns. Transformation indicates
authorization/applicability, not evidence that any row actually changed.

DQ evaluates expectations without producing or transforming data. A rule has
one semantic node and three applicability edges, not a separate rule per zone.
Those edges contain no observed outcome, violations, compliance ratios, or runs.

Relationships are projected once on logical columns, independent of physical
zones. Identical endpoints share one structural edge with combined evidence.
They do not prove effective referential integrity. Replay selection dependencies
do not become flows between different assets. SNAPSHOT represents a structural
layer, so it has no cutoff, date, run, instance path, or row count.

## Evidence and validation

Evidence is an array of references, not copied documents. Each reference has:

```json
{
  "evidence_type": "metadata",
  "path": "metadata/standardization/card.yaml",
  "selector": {"kind": "standardization_policy", "id": "CARD-STD-001"}
}
```

Metadata selectors identify manifest file names, catalog assets/columns,
relationship names, rule/policy IDs, or replay asset names. Framework references
use `evidence_type: framework_contract`, a relative source path, and
`selector: {symbol: <function-or-constant>}`. No line numbers are identities.

The build registers metadata references only while resolving actual validated
declarations. Contract references are registered only after resolving their AST
symbols. Graph validation accepts only these registered references and verifies
that their files still exist within the repository root. Arbitrary selectors or
unavailable files therefore fail resolution. This is build-local resolution of
current declarations, not immutable archival evidence of earlier versions.

Manifest/catalog coverage must be exact; duplicate assets, columns, declaration
IDs, or YAML mapping keys are rejected. Invalid rule/policy scopes, relationships,
replay declarations, conflicting node/edge IDs, endpoints, categories, policy
contexts, or empty/unresolved evidence fail the build. Replay metadata retains
its own dependency validation. No DAG constraint is imposed on the whole graph;
documented logical relationships may form cycles.

## Deterministic publication

Inputs, nodes, edges, and deduplicated evidence arrays are sorted deterministically.
Graph identities use no UUIDs or timestamps. Identical metadata produces identical
logical node/edge sets and IDs, though DuckDB files need not be byte-identical.

A sibling staging database is fully populated, checked through constraints and
counts, checkpointed, and closed before `os.replace` publishes it. Metadata,
graph, staging, or replacement failure before a successful replacement returns
FAILED and preserves the prior database. Staging cleanup is attempted; after
successful replacement, residual staging/WAL cleanup is best-effort and cannot
revert or invalidate the successful publication. Rebuild replaces the projection
rather than appending rows.

A small optional JSON record under `data/results/lineage/runs/` describes only
the graph build: build ID, status, node/edge counts, completion time, duration,
and errors. Its UUID is not a graph identity. No upstream execution records are
read or incorporated. The record is written completely to a sibling temporary
file, flushed and closed, then published with `os.replace`. If writing or
replacement fails, no partial final JSON is published and temporary cleanup is
best-effort. If recording itself fails, the returned result reports
that error and `execution_record: UNAVAILABLE`; the already completed graph
build retains its publication status. This auxiliary record is not part of the
atomic database replacement.

## Acceptance queries

Open the database read-only. A includes related asset flows and the explicit
column associations without inventing column-level flow. The joined context
exposes the policy node and its semantic identifier.

```sql
-- A: structural lineage related to card.issued
SELECT e.edge_category, e.edge_type, s.node_id AS source, t.node_id AS target,
       e.context_node_id, p.semantic_id AS policy_id
FROM lineage_edges e
JOIN lineage_nodes s ON s.node_id = e.source_node_id
JOIN lineage_nodes t ON t.node_id = e.target_node_id
LEFT JOIN lineage_nodes p ON p.node_id = e.context_node_id
WHERE s.asset = 'card' AND (s.node_type = 'asset' OR s.column_name = 'issued')
ORDER BY e.edge_type, s.node_id;

-- B: DQ rules applicable to trans.type
SELECT DISTINCT t.semantic_id AS rule_id
FROM lineage_edges e
JOIN lineage_nodes s ON s.node_id = e.source_node_id
JOIN lineage_nodes t ON t.node_id = e.target_node_id
WHERE e.edge_type = 'evaluated_by' AND s.asset = 'trans' AND s.column_name = 'type'
ORDER BY rule_id;

-- C: columns with explicit transformations
SELECT s.asset, s.column_name, s.zone AS source_zone, t.zone AS target_zone,
       p.semantic_id AS policy_id
FROM lineage_edges e
JOIN lineage_nodes s ON s.node_id = e.source_node_id
JOIN lineage_nodes t ON t.node_id = e.target_node_id
JOIN lineage_nodes p ON p.node_id = e.context_node_id
WHERE e.edge_type = 'transformed_by'
ORDER BY s.asset, s.column_name, policy_id;

-- D: TRUSTED assets flowing into structural SNAPSHOT
SELECT s.asset, s.zone AS source_zone, t.zone AS target_zone
FROM lineage_edges e
JOIN lineage_nodes s ON s.node_id = e.source_node_id
JOIN lineage_nodes t ON t.node_id = e.target_node_id
WHERE e.edge_type = 'replayed_to'
ORDER BY s.asset;

-- E: documented referential relationships of trans
SELECT s.node_id AS source, t.node_id AS target
FROM lineage_edges e
JOIN lineage_nodes s ON s.node_id = e.source_node_id
JOIN lineage_nodes t ON t.node_id = e.target_node_id
WHERE e.edge_type = 'references' AND (s.asset = 'trans' OR t.asset = 'trans')
ORDER BY source, target;

-- F: evidence supporting every edge
SELECT edge_id, edge_category, edge_type, evidence
FROM lineage_edges
ORDER BY edge_id;
```

The current portfolio produces 122 nodes and 93 edges: 8 ingestion, 8 publication,
8 replay, 1 transformation, 60 evaluation, and 8 logical relationship edges.
These counts are derived results, not hard-coded builder assumptions.

## Limits

This projection covers only declared metadata and the explicit current adapters.
It cannot establish executed transformations, effective integrity, row changes,
or historical provenance. Step 7B, upstream execution identities/outcomes, cutoff
instances, temporal trends, row lineage, impact prediction, dashboards, graph UI,
external lineage services, orchestration/cloud integration, new DQ rules/policies,
and remediation are outside Step 7A.
