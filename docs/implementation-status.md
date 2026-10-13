# Roadmap implementation status

Status date: 2026-10-13. This matrix compares the repository with the original six-stage roadmap.
`Partial` means useful code exists but the documented exit criteria have not all passed.

| Stage | Status | Implemented | Remaining exit work |
|---|---|---|---|
| 0. Baseline and skeleton | Partial | Versioned core contracts, LangGraph skeleton, checkpoint wiring, policy gateway, audit events, API, unit and CI gates | 30-case Phase 0 baseline, 20 checkpoint fault-injection recoveries, failed-tool and approval demonstrations, full trace correlation proof |
| 1. Read-only analytics | Partial | TLC source, eight certified metrics, authorized deterministic semantic resolution, read-only SQL firewall, query plan, execution and workflow budgets, immutable artifacts, Supervisor role ledger, validated table/chart/claims, 60-case execution regression, 26-case semantic regression, human-review workbench | Real-model success rate, verified SQL-result accuracy corpus, 15-result human review, and P95 under 60 seconds |
| 2. Data engineering | Not started | Data Engineer role specification only | Second connector, profiling, schema diff, quality rules, transformation proposal, impact analysis, isolated parallel merge, optional approved development write |
| 3. Data science | Not started | Data Scientist role specification only | Python sandbox, snapshot and experiment contracts, reproducible baseline training, leakage/split checks, evaluation report and model card |
| 4. Production readiness | Foundation only | Actor context, tenant/project fields, risk levels, audit sink, fail-closed policy | SSO and row/column policy propagation, short-lived credentials, HITL, DLP, durable audit separation, red-team suite, SLO/alerts/runbooks |
| 5. Business loop | Not started | No external side-effect tools enabled | Per-action approval, idempotency, compensation, kill switch, scheduling, notifications, feedback and cost optimization |

## Current branch scope

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

## Recommended sequence after this branch

1. Close Stage 0 resilience gaps: durable checkpoint resume and fault injection.
2. Qualify an available tool-calling model and run at least 60 representative real-model tasks.
3. Build a value-level SQL oracle corpus, collect reviews for at least 15 successful results, and
   calculate real first-useful-result P95.
4. Start Stage 2 with a read-only second connector and deterministic profiling/schema-diff tools.

Stage 1 implementation is substantially complete, but Stage 1 acceptance is not. The remaining
gates require external model availability and collected evaluation evidence rather than more core
feature scaffolding. Stage 2 can proceed on a separate development branch, but neither Stage 1 nor
Stage 2 should be declared production-ready until the open acceptance gates pass.
