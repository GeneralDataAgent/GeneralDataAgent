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
- A fail-closed compatibility gate permits additive columns, but breaking drift raises a structured
  replanning error; the governed runtime persists the full diff before propagating the failure.
- Constrained not-null, unique, accepted-values, and range rules execute inside the source engine.
- Deterministic quality-rule proposals bind matching schema/profile evidence. Schema non-null
  constraints are errors; snapshot-inferred identifier uniqueness and numeric bounds are warnings.
- Dry-run-only transformation proposals accept one parsed read query, explicit tests, an impact
  summary, and a rollback plan; mutation and unauthorized source references are rejected.
- Deterministic lineage analysis reports directly and transitively impacted assets using stable,
  shortest paths and flags graph cycles.
- Parallel profiling preserves request order, assigns one task-scoped artifact path per request,
  and returns structured partial failures without discarding successful sibling work.
- Batch cancellation prevents queued requests from starting, returns explicit cancelled items, and
  retains completed results; already-running work remains bounded by connector deadlines.
- Runtime cancellation tokens are opaque audit-safe identifiers; the underlying event stays local
  and interrupts active SQLite or DuckDB queries with the shared cancelled error code.
- Every parallel item records submission-to-completion latency, including queue time. A reproducible
  local gate runs 100 requests and requires zero failures, zero cancellations, one isolated artifact
  per success, and an end-to-end P95 no greater than five seconds.
- The optional dbt adapter runs `dbt compile` and approval-gated selected `dbt test` against a pinned
  project and profiles directory, accepts only explicit development or CI targets, validates
  selectors, applies a hard timeout, and redacts local paths from persisted commands and output.
- The optional SQLite development-write runner is absent by default. When explicitly configured for
  `development` or `ci`, it reparses one approved SELECT proposal and creates only a new `dev_`
  table in a pinned database; proposal tests run before commit, exact R2 approval is consumed once,
  the write path must match the registered read connector, and overwrite is prohibited.
- R2 approval requests display exact parameters, impact, cost, classification, rollback, and expiry;
  grants bind the tenant and full request fingerprint, reject self-approval, and are consumed once.
  The approval request and test result are immutable artifacts and consumption is audited.
- The optional SQLite approval store persists requests, grants, and atomic consumption across local
  process restarts; a restart recovery regression proves the original action can resume once and a
  subsequent replay is rejected.
- Rejection closes an unapproved request, while cancellation lets the original requester close any
  unconsumed action. Restart fault-injection verifies rejected, cancelled, consumed, and expired
  states cannot be executed; expiry uses an injected clock rather than timing-dependent sleeps.
- The dbt manifest adapter reads one pinned, size-limited local manifest, records its SHA-256 and dbt
  versions, normalizes sources/models/metrics/exposures into the lineage contract, ignores test-only
  edges, and marks supported but missing dependencies as incomplete instead of silently dropping
  them.
- Profile-oracle comparison checks row count plus per-column null, distinct, minimum, maximum, and
  mean statistics, emitting field-level mismatches with configurable numeric tolerance and a 99%
  default agreement gate.
- A deterministic 20-case cross-engine corpus compares SQLite and DuckDB over empty relations,
  nulls, duplicates, categorical extrema, negative values, and numeric means. All 380 compared
  fields must agree exactly within a `1e-12` numeric tolerance.
- The data-engineering runtime routes inspect, profile, quality, and diff calls through the policy
  gateway, enforces connector-to-domain bindings, and persists immutable JSON artifacts.

## Reproduction

```bash
uv run pytest
uv run ruff check .
uv run python examples/stage2_data_engineering_demo.py
uv run python evals/run_stage1.py
uv run python evals/run_stage2.py
uv run python evals/run_stage2_load.py
uv run python evals/run_stage2_isolation.py
uv run python evals/run_stage2_cross_engine.py
uv run python evals/run_stage2_quality.py
uv run python evals/run_stage2_quality_proposals.py
uv run python evals/run_stage2_impact.py
uv run python evals/run_stage2_schema.py
uv run python evals/run_stage2_transformation.py
uv run python evals/run_stage2_dbt_adapter.py
uv run python evals/run_stage2_development_write.py
uv run python evals/run_stage2_lineage.py
uv run python evals/run_stage2_acceptance.py
```

Expected demo summary:

```text
profile rows=2 columns=3
quality proposal rules=4
quality passed=False violations=1
schema breaking=True changes=3
proposal dry_run_only=True dependencies=1
impact downstream=2 cycles=False
artifacts=8
```

## Acceptance boundary

This is a development foundation, not Stage 2 acceptance. The following remain open:

- real-project dbt compile/test qualification and external approval-service integration;
- authoritative catalog and Dashboard dependency verification beyond the dbt manifest snapshot;
- production-scale load testing beyond the completed cooperative cancellation, reproducible local
  100-request P95 gate, and connector deadline tests;
- comparison with an authoritative external profiler and real projects; cross-engine parity alone
  does not establish independent correctness;
- real source credentials, production identity propagation, and production connector hardening.

No production write path is registered by this runtime.

## Frozen Stage 2 baseline

`evals/datasets/stage2.jsonl` contains 25 deterministic cases: five each for schema drift,
quality rules, lineage impact, transformation safety, and hand-calculated profile oracles. The
runner emits case-level failures and category counts. This baseline is a component regression gate;
it does not replace comparison with an independent authoritative profiler or real dbt projects.

The local load runner creates an ephemeral 1,000-row SQLite source and 100 isolated task requests.
Its five-second P95 threshold is deliberately environment-tolerant for CI; record the observed
latency as diagnostic evidence, not as a production SLO or capacity result.

The isolation runner executes 50 profiling groups concurrently. Each group reads two connectors with
different row counts and must produce the ordered `[3, 7]` result plus two unique artifacts; this
directly tests the 50-group no-cross-talk threshold.

The cross-engine runner creates the same 20 relations in SQLite and DuckDB and compares 380 profile
fields. It is a connector-consistency regression, not an authoritative external-profiler result.

The frozen quality runner contains 20 independently enumerated rule cases over a fixed 10-row
fixture. It requires exact violation counts and final report outcomes for every case; it remains a
deterministic component gate rather than a business-domain rule effectiveness study.

The quality-proposal runner checks 12 conservative-generation boundaries, including severity,
stable identifiers, empty samples, and schema/profile relation, column, type, and row-count
mismatches. Snapshot-derived rules remain warnings until reviewed against business semantics.

The frozen impact runner contains 20 explicit lineage graphs and expected downstream paths. It
checks direct and transitive dependencies, shortest-path tie breaking, multiple changed assets,
disconnected components, cycles, tables, models, metrics, and Dashboards. The authoritative catalog
gate remains `not_run` because synthetic graph recall does not prove real metadata completeness.

The frozen schema runner contains 20 explicit before/after contracts and requires exact ordered
changes plus breaking status. It covers additions, removals, renames, type and nullability changes,
compound drift, connector differences, name/type casing, and ordinal-only changes.

The frozen transformation runner contains eight accepted read-only proposals and twelve expected
rejections. It checks CTEs, joins, unions, qualified relations, dependency extraction, allowlists,
DML/DDL, multi-statement and locking rejection, mandatory tests, and rollback completeness. All
accepted proposals remain `dry_run_only`.

The local fake-dbt runner performs 16 checks for compile/test success and nonzero exits, hard
timeouts, production-target and unsafe-selector rejection, exact and single-use approval binding,
and path redaction in commands, stdout, and stderr. It qualifies adapter behavior only; the
`real_dbt_project` external gate remains `not_run`.

The development-write runner performs 42 checks for default closure, connector-path,
production-environment and unsafe-target rejection, exact one-time approval, generated rollback,
changed-request and replay blocking, no overwrite, correct materialized values, transactional test
rollback, immutable failure evidence, restart-safe durable approval execution, replay blocking after
restart, execution-failure classification and rollback, source preservation, and a false
production-write flag. It also classifies deadline interruption, rolls back the target, persists
failure evidence, and consumes the approval. This local pilot does not authorize or expose
production writes. Concurrent approvals for the same target are serialized: one succeeds, one is
classified as `target_exists`, both approvals are consumed, and task artifacts remain isolated.
It also rejects invalid row budgets and transactionally rolls back results exceeding the configured
limit, which defaults to 100,000 rows.

The frozen dbt-manifest runner contains 20 cases: ten accepted snapshots (including one explicitly
incomplete graph) and ten rejected malformed manifests. Every accepted case is loaded twice to
verify stable hashes, assets, and unresolved dependencies. Authoritative catalog comparison remains
an external `not_run` gate.

The aggregate acceptance runner executes all local development evidence and returns a successful
process status only when those gates pass. Its report still sets `stage_acceptance_complete` to
`false` and lists authoritative profiler, real dbt project, authoritative catalog, external approval
service, and production-scale load gates as `not_run`; local success cannot erase those blockers.
