# Stage 3 data-science workbench validation

Status date: 2026-10-16.

## Implemented slice

- Strict v1 snapshot, split, quota, experiment, check, metric, report, model-card, and registry-draft
  contracts.
- Canonical immutable JSON snapshots with task, tenant, project, and data-domain binding plus
  content-hash verification.
- Fixed-template Python subprocess with isolated mode, no site packages, minimal environment,
  temporary working directory, input/output quotas, wall timeout, and POSIX resource limits.
- Digest-pinned OCI execution contract with pull-never, no network, read-only root, all capabilities
  dropped, no-new-privileges, non-root user, PID/CPU/memory limits, and no-exec temporary storage.
- Restart-safe, append-only SQLite experiment tracking with duplicate-ID rejection and record-hash
  verification on every read.
- Deterministic random, temporal, and group splits for binary classification and regression.
- Majority and mean baselines with EDA, held-out metrics, target-distribution checks, standardized
  mean differences, error analysis, structured subgroup accuracy or MAE, and small-group warnings.
- Fail-closed checks for target leakage, duplicates crossing the split, invalid targets, split
  integrity, schema-role mismatch, task/snapshot mismatch, and metric misuse.
- Complete model-card contract and R2 one-time independent approval for registry drafts. Deployment
  remains structurally impossible.
- Default-off HTTP execution endpoint with immutable snapshot scope enforcement and no code,
  command, package, image, or mount input.

## Deterministic evidence

Run:

```bash
uv run --frozen python evals/run_stage3.py
uv run --frozen python evals/run_stage3_acceptance.py
uv run --frozen pytest tests/test_data_science.py tests/test_stage3_evaluation.py
```

The frozen 20-case gate contains eight accepted experiments and twelve expected rejections. It
covers both problem types, all three split strategies, exact target proxies, cross-split duplicate
leakage, invalid targets, invalid group splitting, metric misuse, byte quotas, and task mismatch.
Success requires every valid case to run and every unsafe case to fail closed.

Without external evidence, `run_stage3_acceptance.py` requires the local gate to pass but reports
the overall stage as `incomplete`. A real hardened-sandbox review can be qualified and signed with:

```bash
uv run --frozen python evals/qualify_stage3_sandbox.py \
  --review /path/to/independent-review.json \
  --output-dir /path/to/evidence

uv run --frozen python evals/attest_stage3_sandbox.py \
  --manifest /path/to/evidence/hardened_sandbox_review.manifest.json \
  --private-key /secure/path/security-reviewer.pem \
  --key-id security-reviewer-2026 \
  --verifier security-reviewer

uv run --frozen python evals/run_stage3_acceptance.py \
  --external-evidence-dir /path/to/evidence \
  --trusted-reviewers /path/to/trusted-reviewers.json
```

At least two distinct trusted reviewer identities must sign the exact manifest. The declared
manifest verifier must be one of them. Keys have explicit activation, expiry, and optional
revocation times; evidence expires after 30 days. Tampering, unknown bundle files, insufficient
signers, stale evidence, invalid control flags, or fewer than 20 successful escape tests fail closed.

## Acceptance boundary

This closes the planned local repository slice; it does not certify a production sandbox. A
hardened container or microVM with network namespace isolation, a read-only image, workload
identity, external experiment tracking/model registry, resource telemetry, subgroup review, and an
independent security assessment remain required before production use. No model deployment path is
present. The external evidence workflow verifies a review; it does not conduct or fabricate one.
The current host has neither Docker nor Podman, so only the OCI build definition and exact
invocation contract were tested here; image construction and escape tests remain external evidence.
