# TLC reproducibility contract

## Reproducibility contract

The preparation command records:

- exact source URLs;
- SHA-256 for the Parquet and zone lookup;
- source period and curated row count;
- preparation timestamp.

Downloaded and generated data remain outside Git. Code, metric definitions, tests and this source
ledger remain versioned.

Dataset selection and source limitations are recorded in the [external planning document](../../../docs/research/open-dataset-selection.md).
