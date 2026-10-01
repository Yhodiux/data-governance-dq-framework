# Local Ingestion Architecture

## Scope

Step 1A publishes the external Berka source files into the local RAW zone only after source-integrity validation. These checks confirm delivery integrity against the approved manifest; they are not business Data Quality rules.

```text
berka-source/berka-dataset/
            |
            v
config/dataset_manifest.yaml -> existence, SHA-256, record-count validation
            |
            v
temporary staging directory -> atomic directory publication -> data/raw/
            |
            v
                              data/results/ingestion/<run_id>.json
```

## Execution behavior

1. Load expected file names, checksums, and record counts from the manifest.
2. Validate every expected source file without changing it.
3. If any validation fails, leave RAW untouched and write a failed execution record.
4. If all validations pass, copy exact file bytes into a temporary directory beside RAW.
5. Replace RAW at directory level. If publication fails, restore the previous RAW directory.
6. Write a timestamped JSON execution record for either outcome.

The implementation does not parse fields, normalize values, interpret empty fields, select a century for two-digit years, or transform source codes.

