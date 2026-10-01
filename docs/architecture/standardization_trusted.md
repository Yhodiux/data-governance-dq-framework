# Policy-Driven Standardization and TRUSTED

## Purpose and governance boundary

Step 5 produces a complete local TRUSTED dataset from RAW by applying only explicitly approved standardization policies:

```text
data/raw/ + metadata/standardization/
                    |
                    v
       policy validation and staging
                    |
                    v
              data/trusted/
```

Data Quality detection does not authorize a transformation. DQ records observed discrepancies; governance must independently approve a policy; TRUSTED applies only that policy. The engine does not inspect DQ results or infer remediation.

TRUSTED means policy-standardized output at this project stage. It does not mean fully clean, universally valid, or compliant with every DQ rule.

## Declarative policy model

Each policy declares:

- unique `id`;
- catalog `asset` and `column`;
- `transformation.type`, `pattern`, and `replacement`;
- catalog evidence with exact `<asset>.<column>` reference and justification.

The only implemented operator is `regex_replace`. It uses full-value matching. A matching value is replaced using the declared regex capture expansion. Nonmatching values remain exactly unchanged. The operator does not trim, search substrings, normalize case or whitespace, parse dates, or alter parser NULLs.

## Authorized Step 5 policy

`CARD-STD-001` applies only to `card.issued`. Values matching exactly `^(\d{6}) 00:00:00$` are changed to the captured six digits. This converts the observed physical representation to the catalog's documented `YYMMDD` representation without accepting arbitrary suffixes or date formats.

No policy authorizes changes to the whitespace values in `order.k_symbol` or `trans.k_symbol`, or to `trans.type = VYBER`. Those values remain untouched evidence awaiting separate governance decisions.

## Physical preservation

All manifest-declared assets are present in every successful publication. Assets without a policy are copied byte-for-byte. For a transformed asset, the engine preserves the original header, delimiter, row order, column order, line endings, field quoting style, and all non-target physical tokens; only authorized target tokens are replaced.

RAW is opened read-only and never modified. DQ is not automatically rerun against TRUSTED.

## Transformation evidence

Each policy result records the policy, target, operator, rows evaluated, rows changed, rows unchanged, and at most five deterministic before/after examples. These are transformation facts, not a quality score or a DQ compliance result.

## All-or-nothing publication

Policies and required RAW assets are validated before publication. The full TRUSTED dataset is built in a sibling staging directory. Staging validation confirms every expected asset, byte equality for copied assets, and header/row-count preservation for transformed assets.

The existing TRUSTED directory is moved to a temporary backup only after staging succeeds. The staged directory is then published at directory level. Any publication failure restores the prior TRUSTED directory and removes temporary residue.

## Current limitations

Only `regex_replace` and one approved policy are implemented. There is no automatic policy inference, DQ revalidation, completeness processing, scoring, remediation workflow, replay, incremental ingestion, dashboard, or cloud publication. Post-standardization DQ revalidation is deliberate future work.

