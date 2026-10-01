# Historical Snapshot Builder — Step 6A

Replay selects original rows from read-only `data/trusted` using an inclusive
ISO cutoff. It publishes all eight manifest assets to
`data/snapshots/<YYYY-MM-DD>/` and writes an execution record to
`data/results/replay/<run-id>.json`.

```bash
python -m src.replay --cutoff 1993-12-31
```

## Metadata and selection

`metadata/replay/historical_snapshot.yaml` defines an `assets` mapping. Each
entry declares a strategy and, where applicable, temporal and reference metadata.
The catalog determines file names and columns; the manifest determines the
expected file set and physical delimiter and quote character. Original SOURCE
checksums and counts are not used to validate TRUSTED.

| Asset | Strategy | Predicate |
|---|---|---|
| account | temporal | date <= cutoff |
| disp | reference | account_id in selected account.account_id |
| client | reference | client_id in selected disp.client_id |
| order | reference | account_id in selected account.account_id |
| trans | temporal_and_reference | date <= cutoff AND selected account |
| loan | temporal_and_reference | date <= cutoff AND selected account |
| card | temporal_and_reference | issued <= cutoff AND selected disp |
| district | static | Complete byte-identical copy |

Temporal declarations contain `column`, `format: YYMMDD`, and `century: 1900`.
The year is explicitly `1900 + YY`; there is no library pivot, no inferred
century, and no restriction to the observed 1993–1998 range. Cutoffs can be any
valid ISO date. Empty, malformed, or impossible temporal values anywhere in the
input fail the build, including rows that would otherwise be excluded.

References declare local `column`, dependency `asset`, `target_column`, and a
named `relationship` from `metadata/relationships.yaml`. Either direction of a
documented relationship is supported. In particular, selecting client depends
on disp even though the source foreign key points from disp to client. Multiple
references are combined with AND.

The engine validates complete catalog/manifest coverage, strategy requirements,
columns, documented temporal formats, reference endpoints and relationship names.
A depth-first traversal with visiting/visited sets detects cycles and produces a
dependency-first order, independent of YAML order. Sorted roots make traversal
deterministic. No asset-specific selection behavior is encoded in the engine.

## Structural contract and evidence

Before selection, all source files are scanned and the full key sets for the
relationships used by replay are validated in their documented foreign-key
direction. Empty foreign keys and orphan references fail even when their rows
would fall outside the cutoff. A reference to a valid source row that was
excluded from its dependency snapshot is an intentional selection exclusion.

Staging is independently scanned to verify dates, counts, headers, complete
file coverage, and the same referential contract. This is construction validation,
not execution of DQ rules. The two district relationships are not selection
references in the approved policy; district is retained in full. Replay does not
introduce additional DQ checks for those relationships.

Execution JSON includes run ID, start/end timestamps, duration, status, cutoff,
TRUSTED zone and resolved source/snapshot paths, the policy and relationship
metadata used, strategy and input/output row counts per completed asset, and
errors. Failed builds may contain only the assets completed before failure.
If the results directory cannot be written, the command fails and reports
`execution_record: UNAVAILABLE`; no process can guarantee disk evidence on an
unwritable filesystem.

## Physical preservation

Files are read as binary physical lines. Headers and selected lines are written
as their original bytes, preserving quoting, delimiters, column order, row order,
LF/CRLF endings and a missing final newline. Static files therefore remain
byte-identical. CSV decoding is used only to evaluate predicates and keys, never
to serialize output. UTF-8 and a UTF-8 BOM in the header are supported.

Like the existing standardization parser, this implementation requires one CSV
record per physical line. Quoted multiline fields are rejected. Binary line
iteration supports LF and CRLF, not files separated exclusively by bare CR.
These limitations match the current dataset. Whitespace and values such as
`VYBER` are retained without trimming, normalization or remediation.

## Safe publication

The builder creates a unique sibling staging directory, selects all assets and
validates staging before touching an existing snapshot. Source hashes are checked
before and after construction. Overlapping input/output locations are rejected.

After validation, an existing snapshot is renamed to a unique backup, and staging
is renamed to the final cutoff directory. Evidence is written before disposing of
the backup. Caught publication or evidence-writing failures restore the previous
snapshot (or remove a new first publication) and clean staging. Rebuilding the
same cutoff is supported and does not accumulate changes.

This follows the repository's directory publication pattern. Individual renames
are atomic on the local filesystem, but replacement of an existing nonempty
directory uses two renames: concurrent readers can briefly see an absent target.
Process termination, power loss, concurrent writers, or a filesystem failure
preventing rollback are not transactionally recoverable by this local pattern.
Runs must be sequential and inputs must not be changed concurrently.

## Historical interpretation and scope

No future leakage means exclusively that published documented temporal values
are no later than the cutoff. This does not reconstruct everything the bank knew
at that date. Client, disp, order and district have no documented historical
validity; selection follows relationships or retains static data. Client birth
numbers are never treated as banking validity dates.

Step 6A does not run DQ over snapshots, extend DQ zones or records, create temporal
observability, dashboards or scores, change DQ or standardization rules, or add
cloud services, streaming or orchestration. DQ on snapshots belongs to Step 6B.
