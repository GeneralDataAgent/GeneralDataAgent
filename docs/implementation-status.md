# Roadmap implementation status

Status date: 2026-10-14. This matrix compares the repository with the original six-stage roadmap.
`Partial` means useful code exists but the documented exit criteria have not all passed.

| Stage | Status | Implemented | Remaining exit work |
|---|---|---|---|
| 0. Baseline and skeleton | Partial | Versioned core contracts, LangGraph skeleton, checkpoint wiring, policy gateway, audit events, API, unit and CI gates | 30-case Phase 0 baseline, 20 checkpoint fault-injection recoveries, failed-tool and approval demonstrations, full trace correlation proof |
| 1. Read-only analytics | Partial | TLC source, eight certified metrics, authorized deterministic semantic resolution, read-only SQL firewall, query plan, execution and workflow budgets, immutable artifacts, Supervisor role ledger, validated table/chart/claims, 60-case execution regression, 26-case semantic regression, human-review workbench | Real-model success rate, verified SQL-result accuracy corpus, 15-result human review, and P95 under 60 seconds |
| 2. Data engineering | Partial | Unified read-only SQLite and DuckDB connectors, capability declarations, normalized errors, deadlines and cooperative cancellation, deterministic profiling, schema diff, quality rules, dry-run transformation proposals, deterministic impact analysis, isolated parallel profiling with queued-work cancellation and a reproducible 100-request local P95 gate, pinned non-production dbt compile plus R2-gated selected-test adapters, default-off R2 SQLite development-write pilot, restart-safe local approval store, hashed dbt-manifest lineage ingestion, field-level profile oracle comparison, 20-case/380-field cross-engine parity gate, frozen 25-case component baseline, policy-gated runtime, immutable artifacts | Real-project dbt compile/test qualification, external approval service integration, authoritative catalog and external-profiler comparison, production-scale load tests |
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
- Quality-rule proposals bind matching schema/profile evidence: schema non-null constraints are
  errors, while observed identifier uniqueness and numeric ranges remain reviewable warnings.
- Schema drift is explicit and marks removals, type changes, and nullability changes as breaking.
- The schema compatibility gate permits additive drift but stops on every breaking change, carries
  the full diff in a structured replanning error, and persists that evidence before returning the
  failure.
- Transformation proposals are parsed read queries with explicit tests, impact, and rollback; they
  cannot execute writes.
- Lineage impact analysis returns stable shortest paths to downstream models, metrics, and
  Dashboards and flags cycles.
- Parallel profiling preserves request order, isolates task-scoped artifacts, and retains successful
  results when sibling requests fail; the regression suite covers 50 concurrent requests.
- A separate 50-group concurrent gate alternates two source sizes and verifies 100 distinct task
  artifacts, directly covering the Stage 2 isolation threshold without claiming production load.
- The frozen Stage 2 baseline covers 25 schema, quality, impact, transformation safety, and
  hand-calculated profile-oracle cases.
- Profile comparison emits field-level mismatches and applies numeric tolerance plus a configurable
  agreement threshold that defaults to the Stage 2 99% target.
- A deterministic 20-case corpus spans empty relations, nulls, duplicates, strings, negative values,
  and numeric means; SQLite and DuckDB currently agree on all 380 compared profile fields.
- A separate frozen 20-case quality matrix verifies exact violation counts and report status across
  not-null, uniqueness, accepted-value, range, and warning-severity behavior.
- A 12-check proposal-safety gate verifies conservative severity, deterministic rule identifiers,
  empty-sample behavior, and fail-closed schema/profile mismatch handling.
- A frozen 20-case impact matrix verifies exact downstream asset types, distances, and shortest
  paths across direct/transitive chains, fan-in, multiple changes, disconnected nodes, and cycles.
- A frozen 20-case schema matrix verifies exact change ordering and breaking classification across
  additions, removals, renames, type/nullability changes, and compound drift.
- A frozen 20-case transformation matrix verifies dependency extraction for safe reads and rejection
  of unauthorized relations, DML, DDL, multiple statements, locks, and incomplete proposals.
- A machine-readable acceptance report aggregates the frozen component, cross-engine, local load,
  schema fail-closed, and exactly-once approval gates. It cannot report Stage 2 complete while the
  declared real-environment gates remain `not_run`.
- SQLite and DuckDB connectors enforce per-query deadlines and normalize interruption as retryable
  timeout failures instead of leaving profiling calls unbounded.
- Batch cancellation prevents queued profile requests from starting, preserves completed siblings,
  and reports cancelled items explicitly; active requests remain bounded by connector deadlines.
- Cancellation tokens pass through the policy-gated runtime without exposing event objects in audit
  payloads and interrupt active SQLite and DuckDB queries with a normalized cancelled result.
- Batch items record end-to-end latency from submission, including queue time; the reproducible local
  gate requires 100 successes, 100 isolated artifacts, no cancellations, and P95 at or below five
  seconds.
- The optional dbt adapter pins project and profiles paths, rejects production targets and unsafe
  selectors, and enforces a timeout. Compile is R1; selected `dbt test` is R2 and remains unavailable
  unless a separate approval store is configured.
- A 16-check fake-dbt qualification covers success, nonzero exits, timeout, target/selector guards,
  exact approvals, replay blocking, and path redaction in commands, stdout, and stderr. Real-project
  qualification remains open.
- The optional SQLite development-write runner is absent by default, accepts only explicit
  `development`/`ci` configuration and new `dev_` targets, reparses the SELECT proposal, generates
  the rollback statement, consumes exact R2 approval once, runs proposal tests transactionally, and
  never exposes production writes.
- Its 42-check gate covers default closure, connector-path/environment/target restrictions,
  changed-request and replay rejection, no overwrite, exact result values, test rollback, source
  preservation, immutable failure evidence, durable approval execution across restart, and
  execution-error rollback after approved-source drift, deadline interruption, and exactly-once
  target creation under concurrent approvals, plus a configurable transactional row budget.
- Approval requests expose the exact action, target, parameters, impact, cost, classification,
  rollback, and expiry. Grants bind the full request fingerprint and tenant, reject self-approval,
  expire, and are consumed once before execution; request and execution reports are immutable
  artifacts and successful consumption is an audit event.
- The SQLite approval store atomically persists request, grant, and consumption state, so an approved
  task can resume after a local process restart without making the grant replayable. The in-memory
  store remains available only for isolated tests and ephemeral development.
- Reviewers can reject pending requests and requesters can cancel any unconsumed action, including an
  approved one. Rejection, cancellation, consumption, and expiry are mutually exclusive execution
  boundaries that remain effective after restart; deterministic injected-clock tests cover expiry.
- The dbt manifest adapter pins and hashes its input, normalizes supported assets, and makes missing
  dependencies explicit before impact analysis.
- A frozen 20-case manifest matrix verifies supported node normalization, dependency filtering,
  incomplete snapshots, deterministic re-ingestion, and malformed-input rejection.
- All engineering operations pass through the policy gateway, enforce connector-domain bindings,
  and persist immutable artifacts.
- The Stage 2 acceptance runner can ingest bounded external evidence bundles with exact gate IDs,
  content hashes, relative-path confinement, verifier/source metadata, and expiry. It remains
  fail-closed and defaults every external gate to `not_run` when no evidence directory is supplied.
- A real-dbt qualification command runs compile and selected tests against an explicitly supplied
  non-production project and emits the corresponding hashed evidence pair without overwriting prior
  evidence. No project, credentials, or environment are bundled, so the real gate remains open.
- An authoritative-profiler qualification command compares strict profile JSON from explicitly
  independent source systems at the 99% default threshold and emits hashed pass/fail evidence. The
  repository still needs a real authoritative profile export to close that gate.
- An authoritative-catalog qualification command checks selected critical assets, direct
  dependencies, and downstream Dashboard paths between complete independently sourced graphs and
  emits hashed pass/fail evidence. A real catalog export is still required to close the gate.
- An external-approval qualification command validates a strict service transcript for independent
  approval, exact request and tenant binding, expiry, rejection paths, one-time consumption, and
  replay blocking. A real independently exported transcript is still required to close the gate.
- A production-load qualification command validates staging or production measurements for request
  scale, target throughput, errors, cancellations, artifacts, P95 latency, saturation, backpressure,
  and bounded recovery. A real target-environment transcript is still required to close the gate.
- External acceptance bundles now require detached Ed25519 attestations from explicitly trusted
  verifier keys. The trust registry binds each verifier and key ID to an exact gate allowlist, so
  unsigned, untrusted, unauthorized, or post-signature-tampered evidence fails closed.
- Trusted verifier keys carry activation, expiry, and optional revocation timestamps. Attestations
  cryptographically bind their signing time; verification rejects pre-activation, future-dated,
  post-evidence-expiry, and revoked signatures while preserving evidence signed during a rotated
  key's former validity window.

## Recommended sequence after this increment

1. Close Stage 0 resilience gaps: durable checkpoint resume and fault injection.
2. Qualify an available tool-calling model and run at least 60 representative real-model tasks.
3. Build a value-level SQL oracle corpus, collect reviews for at least 15 successful results, and
   calculate real first-useful-result P95.
4. Qualify dbt compile and the R2-gated selected-test path on a real project, replace the local
   local approval store with an external workflow, and verify manifest lineage against an
   authoritative catalog.
5. Compare profiles with an authoritative tool and add production-scale load tests.

Stage 1 implementation is substantially complete, but Stage 1 acceptance is not. The remaining
gates require external model availability and collected evaluation evidence rather than more core
feature scaffolding. Stage 2 is now under active development, but neither Stage 1 nor Stage 2
should be declared production-ready until the open acceptance gates pass.
