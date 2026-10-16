# Stage 3 data-science workbench validation

Status date: 2026-10-15.

## Implemented slice

- Strict v1 snapshot, split, quota, experiment, check, metric, report, model-card, and registry-draft
  contracts.
- Canonical immutable JSON snapshots with task binding and content-hash verification.
- Fixed-template Python subprocess with isolated mode, no site packages, minimal environment,
  temporary working directory, input/output quotas, wall timeout, and POSIX resource limits.
- Restart-safe, append-only SQLite experiment tracking with duplicate-ID rejection and record-hash
  verification on every read.
- Deterministic random, temporal, and group splits for binary classification and regression.
- Majority and mean baselines with EDA, held-out metrics, target-distribution checks, standardized
  mean differences, and error analysis.
- Fail-closed checks for target leakage, duplicates crossing the split, invalid targets, split
  integrity, schema-role mismatch, task/snapshot mismatch, and metric misuse.
- Complete model-card contract and R2 one-time independent approval for registry drafts. Deployment
  remains structurally impossible.

## Deterministic evidence

Run:

```bash
uv run --frozen python evals/run_stage3.py
uv run --frozen pytest tests/test_data_science.py tests/test_stage3_evaluation.py
```

The frozen 20-case gate contains eight accepted experiments and twelve expected rejections. It
covers both problem types, all three split strategies, exact target proxies, cross-split duplicate
leakage, invalid targets, invalid group splitting, metric misuse, byte quotas, and task mismatch.
Success requires every valid case to run and every unsafe case to fail closed.

## Acceptance boundary

This closes the planned local repository slice; it does not certify a production sandbox. A
hardened container or microVM with network namespace isolation, a read-only image, workload
identity, external experiment tracking/model registry, resource telemetry, subgroup review, and an
independent security assessment remain required before production use. No model deployment path is
present.
