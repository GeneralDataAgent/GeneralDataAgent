# Stage 2 data-engineering foundation validation

Validation date: 2026-10-13.

## Scope

This increment establishes the first read-only Stage 2 vertical slice:

- SQLite and DuckDB implement one connector interface and normalized error model.
- Both connectors enforce query deadlines and report engine interruption through the shared,
  retryable timeout error code.
- Every connector declares immutable capabilities; both shipped connectors explicitly disable DML
  and DDL.
- Schema introspection produces one versioned `RelationSchema` contract.
- Deterministic profiling calculates row count, null count, distinct count, minimum, maximum, and
  numeric mean.
- Deterministic schema diff identifies added, removed, type-changed, and nullability-changed columns.
- Constrained not-null, unique, accepted-values, and range rules execute inside the source engine.
- Dry-run-only transformation proposals accept one parsed read query, explicit tests, an impact
  summary, and a rollback plan; mutation and unauthorized source references are rejected.
- Deterministic lineage analysis reports directly and transitively impacted assets using stable,
  shortest paths and flags graph cycles.
- Parallel profiling preserves request order, assigns one task-scoped artifact path per request,
  and returns structured partial failures without discarding successful sibling work.
- The optional dbt adapter runs only `dbt compile` against a pinned project and profiles directory,
  accepts only explicit development or CI targets, validates selectors, applies a hard timeout, and
  redacts local paths from the persisted command report.
- The dbt manifest adapter reads one pinned, size-limited local manifest, records its SHA-256 and dbt
  versions, normalizes sources/models/metrics/exposures into the lineage contract, ignores test-only
  edges, and marks supported but missing dependencies as incomplete instead of silently dropping
  them.
- Profile-oracle comparison checks row count plus per-column null, distinct, minimum, maximum, and
  mean statistics, emitting field-level mismatches with configurable numeric tolerance and a 99%
  default agreement gate.
- The data-engineering runtime routes inspect, profile, quality, and diff calls through the policy
  gateway, enforces connector-to-domain bindings, and persists immutable JSON artifacts.

## Reproduction

```bash
uv run pytest
uv run ruff check .
uv run python examples/stage2_data_engineering_demo.py
uv run python evals/run_stage1.py
uv run python evals/run_stage2.py
```

Expected demo summary:

```text
profile rows=2 columns=3
quality passed=False violations=1
schema breaking=True changes=3
proposal dry_run_only=True dependencies=1
impact downstream=2 cycles=False
artifacts=7
```

## Acceptance boundary

This is a development foundation, not Stage 2 acceptance. The following remain open:

- real-project dbt compile qualification and R2-gated dbt test execution;
- authoritative catalog and Dashboard dependency verification beyond the dbt manifest snapshot;
- batch-level cancellation and production-scale load testing beyond the completed 50-request
  isolation regression and connector deadline tests;
- comparison of the frozen Stage 2 corpus with an authoritative profiler and real projects;
- optional R2 development-write approval trial, which remains disabled;
- real source credentials, production identity propagation, and production connector hardening.

No production write path is registered by this runtime.

## Frozen Stage 2 baseline

`evals/datasets/stage2.jsonl` contains 25 deterministic cases: five each for schema drift,
quality rules, lineage impact, transformation safety, and hand-calculated profile oracles. The
runner emits case-level failures and category counts. This baseline is a component regression gate;
it does not replace comparison with an independent authoritative profiler or real dbt projects.
