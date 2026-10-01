# Local Profiling Architecture

## Purpose and flow

Step 1B measures the physical contents of the successfully ingested RAW dataset. It follows the principle **measure first, interpret later**.

```text
data/raw/ + config/dataset_manifest.yaml
                    |
                    v
       DuckDB physical-text profiling
                    |
                    v
data/results/profiling/<run_id>.json
```

The manifest supplies the expected file list and parser settings. If any declared RAW file is missing, the run fails before profiling begins. The profiler never reads `berka-source/` and opens RAW only for reading.

## Metric definitions

DuckDB reads every source column as `VARCHAR`; inferred numeric or date types are deliberately disabled.

- `row_count`: number of parsed data rows, excluding the header.
- `column_count`: number of physical header columns exposed by the parser.
- `total_count`: table row count, repeated for the column.
- `empty_count`: parsed values whose physical text has zero characters.
- `parser_null_count`: values returned as SQL `NULL` by the parser. A non-source NUL marker is configured as the technical NULL token so ordinary empty fields remain empty strings.
- `non_empty_count`: values that are neither zero-length strings nor parser `NULL` values.
- `distinct_count`: number of distinct non-empty textual values.
- `uniqueness_ratio`: `distinct_count / non_empty_count`; JSON `null` when `non_empty_count` is zero.
- `min_observed` and `max_observed`: lexicographic minima and maxima among non-empty textual values. They are not numeric or date extrema.
- `sample_values`: at most five distinct, non-empty values sorted lexicographically. They are deterministic evidence, not a statistically representative sample.

## Empty-value behavior and limitations

The parser is configured to retain ordinary empty fields as zero-length strings rather than silently mapping them to SQL `NULL`. Consequently, `empty_count` describes a physical parser result and does not assert business nullness.

DuckDB's parsed output collapses a quoted empty field (`""`) and an unquoted empty delimited field (`;;`) to the same zero-length string. The profiler therefore counts them together and cannot report their separate frequencies. It does not interpret either representation as a missing business value. Embedded NUL bytes are outside the established text-source format and would be exposed through `parser_null_count` if encountered.

## Interpretation boundary

Profiling findings describe observed data only. Empty values, repeated values, uniqueness ratios, lexical ranges, and patterns do not by themselves establish keys, requirements, validity, defects, or Data Quality rules. Those decisions belong to later governance and Data Quality phases.

