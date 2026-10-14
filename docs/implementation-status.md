# Roadmap implementation status

Status date: 2026-10-14. This matrix compares the repository with the original six-stage roadmap.
`Partial` means useful code exists but the documented exit criteria have not all passed.

| Stage | Status | Implemented | Remaining exit work |
|---|---|---|---|
| 0. Baseline and skeleton | Partial | Versioned core contracts, LangGraph skeleton, checkpoint wiring, policy gateway, audit events, API, unit and CI gates | 30-case Phase 0 baseline, 20 checkpoint fault-injection recoveries, failed-tool and approval demonstrations, full trace correlation proof |
| 1. Read-only analytics | Partial | TLC source, eight certified metrics, authorized deterministic semantic resolution, read-only SQL firewall, query plan, execution and workflow budgets, immutable artifacts, Supervisor role ledger, validated table/chart/claims, 60-case execution regression, 26-case semantic regression, human-review workbench | Real-model success rate, verified SQL-result accuracy corpus, 15-result human review, and P95 under 60 seconds |
| 2. Data engineering | Partial | Unified read-only SQLite and DuckDB connectors, capability declarations, normalized errors and query deadlines, deterministic profiling, schema diff, quality rules, dry-run transformation proposals, deterministic impact analysis, isolated parallel profiling with 50-request regression and queued-work cancellation, pinned non-production dbt compile adapter, hashed dbt-manifest lineage ingestion, field-level profile oracle comparison, frozen 25-case component baseline, policy-gated runtime, immutable artifacts | Real-project dbt compile qualification, R2-gated dbt test, authoritative catalog and external-profiler comparison, active-query cooperative cancellation and production-scale load tests, optional approved development write |
| 3. Data science | Not started | Data Scientist role specification only | Python sandbox, snapshot and experiment contracts, reproducible baseline training, leakage/split checks, evaluation report and model card |
| 4. Production readiness | Foundation only | Actor context, tenant/project fields, risk levels, audit sink, fail-closed policy | SSO and row/column policy propagation, short-lived credentials, HITL, DLP, durable audit separation, red-team suite, SLO/alerts/runbooks |
| 5. Business loop | Not started | No external side-effect tools enabled | Per-action approval, idempotency, compensation, kill switch, scheduling, notifications, feedback and cost optimization |

## Completed Stage 1 branch scope

`feat/stage1-trusted-reporting` closes the planned Stage 1 implementation gaps without expanding
data access:

- `TableSpec`, `ChartSpec`, `GroundedClaim`, and `DeliveryPackage` are strict v1 contracts.
- Chart instructions reference the immutable query artifact instead of copying data.
- Every delivered claim cites evidence created by the same task.
- The validation gate blocks missing chart artifacts and ungrounded claims.
- The TLC catalog exposes eight certified metrics over one approved grouping dimension.
- English and Chinese aliases resolve deterministically; shared aliases return an explicit
  ambiguity response instead of selecting a metric.
- Semantic search is filtered by the actor's authorized domains before candidates are returned.
- The frozen 60-case execution and 26-case semantic baselines produce case-level results.
- Human reviews are append-only, reject duplicate reviewers, and expose an explicit pass summary.
- Anthropic-compatible and OpenAI Responses-compatible models can be selected by stable CC Switch
  provider ID without copying credentials into the repository.
- The Supervisor has a whole-workflow deadline in addition to per-request provider timeouts.

## Current Stage 2 branch scope

`feat/stage2-data-engineering-foundation` starts Stage 2 without widening mutation authority:

- SQLite and DuckDB share a read-only connector protocol and normalized error codes.
- Connectors publish immutable capabilities and cannot claim DML or DDL while read-only.
- Versioned relation schema, data profile, schema diff, quality rule, and quality report contracts
  reject malformed or internally inconsistent results.
- Profiling and quality checks execute deterministic source-side aggregation queries.
- Schema drift is explicit and marks removals, type changes, and nullability changes as breaking.
- Transformation proposals are parsed read queries with explicit tests, impact, and rollback; they
  cannot execute writes.
- Lineage impact analysis returns stable shortest paths to downstream models, metrics, and
  Dashboards and flags cycles.
- Parallel profiling preserves request order, isolates task-scoped artifacts, and retains successful
  results when sibling requests fail; the regression suite covers 50 concurrent requests.
- The frozen Stage 2 baseline covers 25 schema, quality, impact, transformation safety, and
  hand-calculated profile-oracle cases.
- Profile comparison emits field-level mismatches and applies numeric tolerance plus a configurable
  agreement threshold that defaults to the Stage 2 99% target.
- SQLite and DuckDB connectors enforce per-query deadlines and normalize interruption as retryable
  timeout failures instead of leaving profiling calls unbounded.
- Batch cancellation prevents queued profile requests from starting, preserves completed siblings,
  and reports cancelled items explicitly; active requests remain bounded by connector deadlines.
- The optional dbt adapter pins project and profiles paths, rejects production targets and unsafe
  selectors, enforces a timeout, and only exposes `compile`; no dbt test or run path is registered.
- The dbt manifest adapter pins and hashes its input, normalizes supported assets, and makes missing
  dependencies explicit before impact analysis.
- All engineering operations pass through the policy gateway, enforce connector-domain bindings,
  and persist immutable artifacts.

## Recommended sequence after this increment

1. Close Stage 0 resilience gaps: durable checkpoint resume and fault injection.
2. Qualify an available tool-calling model and run at least 60 representative real-model tasks.
3. Build a value-level SQL oracle corpus, collect reviews for at least 15 successful results, and
   calculate real first-useful-result P95.
4. Qualify dbt compile on a real project, add R2-gated dbt test, and verify manifest lineage against
   an authoritative catalog.
5. Compare profiles with an authoritative tool and add active-query cooperative cancellation plus
   production-scale load tests.

Stage 1 implementation is substantially complete, but Stage 1 acceptance is not. The remaining
gates require external model availability and collected evaluation evidence rather than more core
feature scaffolding. Stage 2 is now under active development, but neither Stage 1 nor Stage 2
should be declared production-ready until the open acceptance gates pass.
