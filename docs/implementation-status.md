# Roadmap implementation status

Status date: 2026-10-19. This matrix compares the repository with the original six-stage roadmap.
`Partial` is an acceptance status: useful code exists but the documented exit criteria have not all
passed. It does not necessarily mean planned repository implementation is missing.

| Stage | Status | Implemented | Remaining exit work |
|---|---|---|---|
| 0. Baseline and skeleton | Partial | Versioned core contracts, LangGraph skeleton, checkpoint wiring, policy gateway, audit events, API, unit and CI gates | 30-case Phase 0 baseline, 20 checkpoint fault-injection recoveries, failed-tool and approval demonstrations, full trace correlation proof |
| 1. Read-only analytics | Partial | TLC source, eight certified metrics, authorized deterministic semantic resolution, read-only SQL firewall, query plan, execution and workflow budgets, immutable artifacts, Supervisor role ledger, validated table/chart/claims, 60-case execution regression, 26-case semantic regression, human-review workbench | Real-model success rate, verified SQL-result accuracy corpus, 15-result human review, and P95 under 60 seconds |
| 2. Data engineering | Partial | Unified read-only SQLite and DuckDB connectors, capability declarations, normalized errors, deadlines and cooperative cancellation, deterministic profiling, schema diff, quality rules, dry-run transformation proposals, deterministic impact analysis, isolated parallel profiling with queued-work cancellation and a reproducible 100-request local P95 gate, pinned non-production dbt compile plus R2-gated selected-test adapters, default-off R2 SQLite development-write pilot, restart-safe local approval store, hashed dbt-manifest lineage ingestion, field-level profile oracle comparison, 20-case/380-field cross-engine parity gate, frozen 25-case component baseline, policy-gated runtime, immutable artifacts | Real-project dbt compile/test qualification, external approval service integration, authoritative catalog and external-profiler comparison, production-scale load tests |
| 3. Data science | Partial | Data Scientist role, immutable snapshots, strict experiment contracts, fixed-template bounded Python sandbox, deterministic classification/regression baselines, EDA/statistical/evaluation/error-analysis output, leakage/split/metric gates, complete model cards, approval-gated registry drafts, append-only experiment tracking, frozen 20-case gate, signed multi-party hardened-sandbox evidence gate | Independently produce and sign a real hardened-sandbox review; add workload identity, external experiment tracker/registry, and domain subgroup review |
| 4. Production readiness | Partial: local development controls verified; production admission blocked | OIDC code/PKCE + pinned identity verification, explicit action grants, shared short-lived sessions/revocation, scoped durable reviews, original-thread approval resume, native-source leases and PostgreSQL RLS/column-ACL adapter, semantic JSON/Unicode DLP, artifact retention/holds, encrypted checkpoints with concurrent-resume fencing, independent signed audit collector/transport, 91-case security baseline, atomic task budgets, SLO/critical-alert measurements and background retry/retention maintenance, dual-signed live release gate, CI and operations/recovery guide | Real provider enrollment and independent production PostgreSQL credential/RLS/CLS/TLS qualification, independently deployed audit/alert/state infrastructure, hardened sandbox and real security/provider/DR drills, 90+ real-work benchmark, observed monthly SLO and 14 clean pilot days, independently signed admission evidence; see [Stage 4 evidence](validation/stage4-production-readiness.md) |
| 5. Business loop | Partial: local workflows and one Feishu receiver smoke verified; production acceptance blocked | Six strict business draft kinds, durable exact approvals/idempotency/expiry/cancellation/compensation, authenticated API, per-action switches/owners/rate limits, revision guards and receipts, bounded report schedules, feedback curation/holdout governance, versioned capability admission, analysis-integrated metered narration/cache, refreshed report snapshots, enforced extension assembly, configured Feishu notification and frozen-report IM delivery/readback/reconciliation, schedule-bound service-JWT sessions, exact-approved GitHub JSON schedule draft PRs/readback/reconciliation/closure, shared orchestration budgets, measured optimization/release proof verification, 42-case local baseline and dual-signed per-action external qualification | Remaining real receiver adapters, independent Feishu recall/receiver qualification, production audit/retention/review integration, measured business and cost/latency gains, and Stage 4 production admission; see [Stage 5 evidence](validation/stage5-business-loop.md) |

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
- Per-gate attestation policies can require multiple distinct verifier identities. Deterministic
  key-addressed attestation files support independent review, while missing signatures, duplicate
  verifier identities, impossible registry thresholds, and omission of the manifest verifier all
  fail closed; omitted policies retain the original one-verifier behavior.
- Completed external bundles advance a durable SQLite acceptance ledger in one transaction. Exact
  bundle replay is idempotent, while an older observation or a different signed manifest at the same
  observation time is rejected as rollback or equivocation; incomplete bundles do not alter state.
  Each accepted bundle is also appended to a deterministic hash chain. The full chain and its match
  with the current gate snapshot are verified before every write, exposing history or snapshot edits.

## Stage 2 implementation closure

The repository implementation planned for Stage 2 is complete as of 2026-10-15. Every Stage 2
delivery item in the original roadmap now has code, deterministic validation, and an aggregate local
gate: the second connector, unified capabilities/errors, quality-rule generation and execution,
reproducible transformation proposals with tests/impact/rollback, isolated parallel profiling, and
the default-off R2 development-write pilot. The current suite has 181 passing tests and all 14 local
Stage 2 development gates pass.

Stage 2 acceptance remains `Partial` because five exit metrics require independently produced,
real-environment evidence. The repository includes strict qualifiers for all five and a fail-closed
ingestion chain with hashes, Ed25519 attestations, verifier allowlists and lifecycles, optional
multi-party thresholds, rollback/equivocation protection, and an append-only audit chain. It does not
ship or fabricate the external projects, credentials, exports, service transcripts, or production
measurements needed to run them:

1. an authoritative profiler export for the 99% comparison;
2. a real non-production dbt project and approved selected tests;
3. an authoritative catalog export including Dashboard dependencies;
4. an independently operated approval-service transcript;
5. a staging or production capacity, saturation, and recovery transcript.

Until those inputs are supplied and signed by trusted reviewers, the corresponding gates correctly
remain `not_run` and Stage 2 must not be described as production-ready.

## Current Stage 3 branch scope

`feat/stage3-production-governance` starts the data-science workbench without adding arbitrary code
execution or model deployment:

- Dataset snapshots are canonical, immutable, task-bound, content-addressed artifacts with explicit
  tenant/project/data-domain scope and explicit feature, target, group, timestamp, and ignored
  column roles.
- Experiment contracts pin the snapshot, problem type, features, target, baseline, primary metric,
  split, seed, and resource quota. Invalid metric/problem combinations fail during validation.
- The sandbox accepts no code or command. It runs one repository-owned standard-library template in
  isolated Python mode, without site packages, in a temporary working directory and minimal
  environment, with wall-clock, CPU, memory, file, descriptor, and byte limits.
- The runtime fingerprint binds the fixed worker bytes and Python runtime version. Every result is
  stored as an immutable artifact descended from the exact snapshot.
- The optional SQLite experiment tracker is append-only, survives process restart, rejects duplicate
  experiment identifiers, verifies every record hash on read, and lists runs by exact snapshot.
- A digest-only OCI runtime path accepts Docker or Podman and hard-codes pull-never, no-network,
  read-only-root, capability-drop, no-new-privileges, non-root-user, PID/CPU/memory, and no-exec
  temporary-filesystem controls. It exposes no arbitrary mounts, environment, commands, or code.
- The checked-in OCI build pins the Docker Official Python 3.12.13 slim Linux/amd64 image digest and
  copies only the fixed worker. A Linux/amd64 Colima Docker VM built the image and passed the frozen
  20-case matrix plus 24 executable isolation and escape checks on 2026-10-16.
- Deterministic random, temporal, and group splits support majority-classification and
  mean-regression baselines. Output includes EDA, held-out metrics, target distributions,
  standardized mean differences, error analysis, and structured held-out subgroup accuracy or MAE.
- Declared subgroup columns must use snapshot group roles. Small held-out groups emit explicit
  warnings without inventing a universal fairness threshold, and model cards require a subgroup
  evaluation statement.
- Pre-execution gates reject exact target proxies, identical examples crossing the train/test
  boundary, invalid targets, incorrect schema roles, broken split semantics, task/snapshot mismatch,
  and metric misuse.
- Policy allows only the fixed experiment action for the Data Scientist role and records correlated
  decision/completion or decision/failure audit events when an audit sink is configured.
- The optional HTTP experiment endpoint is disabled by default. When configured, it accepts only
  strict snapshot and experiment contracts, enforces caller scope against immutable snapshot
  tenant/project/domain fields, and returns 403/409/422 without widening sandbox authority.
- The model-card contract requires intended use, excluded uses, training and evaluation evidence,
  metrics, ethics, limitations, monitoring, and ownership. Deployment approval is structurally
  fixed to false.
- Model registry submission creates only an R2 draft. It requires an exact, expiring, one-time
  approval from someone other than the requester; the policy exposes no deployment action.
- A frozen 20-case gate covers eight valid experiments and twelve fail-closed negative cases across
  both problem types and all split strategies.
- The aggregate acceptance runner keeps local development evidence separate from the external
  hardened-sandbox gate. A real review must attest a fixed image digest, non-root execution,
  read-only root filesystem, default-deny networking, syscall filtering, no-new-privileges,
  enforced resource limits, a secretless workload, rejection of arbitrary code, and at least 20
  successful escape tests.
- Hardened-sandbox evidence is content hashed, expires, rejects future-dated or tampered bundles,
  and requires Ed25519 signatures from at least two distinct trusted reviewers, including the
  review manifest's declared verifier. Unknown evidence and attestation files fail closed.

Stage 3 repository implementation is intentionally `Partial`: the local process is not a production
security boundary, while the digest-pinned container path now has repeatable local runtime evidence.
Workload identity, external experiment tracking and model registry, domain subgroup review, durable
resource telemetry, and an independent sandbox security assessment remain required before the
roadmap exit condition can pass. The repository contains strict qualification, attestation, and
aggregation paths, but does not fabricate the external review itself.

## Recommended sequence after this increment

1. Close Stage 0 resilience gaps: durable checkpoint resume and fault injection.
2. Qualify an available tool-calling model and run at least 60 representative real-model tasks.
3. Build a value-level SQL oracle corpus, collect reviews for at least 15 successful results, and
   calculate real first-useful-result P95.
4. Qualify dbt compile and the R2-gated selected-test path on a real project, replace the local
   local approval store with an external workflow, and verify manifest lineage against an
   authoritative catalog.
5. Compare profiles with an authoritative tool and add production-scale load tests.
6. Qualify the Stage 3 fixed template in a hardened sandbox and complete independent security and
   subgroup reviews before connecting any external tracker or registry.

Stage 1 implementation is substantially complete, but Stage 1 acceptance is not. Stage 2 repository
implementation is complete, but its external gates remain open. Stage 3 local implementation has
started and its deterministic gate passes; none of these stages should be described as
production-ready until their documented external and security acceptance gates pass.


## Stage 5 development branch

`feat/stage5-business-loop` starts the business loop with the local workflows, governance and
qualification tooling mapped in [Stage 5 evidence](validation/stage5-business-loop.md). All 42 frozen
local action cases pass and the current full suite passes (453 passed, one opt-in PostgreSQL skip;
87.84% coverage). Review and operation steps are in the [Stage 5 runbook](operations/stage5-runbook.md).
External effects are disabled by default; a configured Feishu pilot sent one user-approved bot-to-self
notification and verified its receiver content, with replay returning the original receipt. A GitHub
JSON scheduling draft-PR receiver now has isolated validation for exact approved changes, readback,
no-resend quarantine, read-only reconciliation and separate closure approval. Real GitHub target
enrollment, Dashboard/ticket/registry receivers, Feishu cloud-document/file publication and production
service activation remain open.
Frozen report IM publication and short-lived service-JWT identity adapters have isolated end-to-end
validation; their live target/provider enrollment remains separate. This implementation does not close independent production
or business-value acceptance and does not change earlier stages' outstanding external gates.
