# Roadmap implementation status

Status date: 2026-10-13. This matrix compares the repository with the original six-stage roadmap.
`Partial` means useful code exists but the documented exit criteria have not all passed.

| Stage | Status | Implemented | Remaining exit work |
|---|---|---|---|
| 0. Baseline and skeleton | Partial | Versioned core contracts, LangGraph skeleton, checkpoint wiring, policy gateway, audit events, API, unit and CI gates | 30-case Phase 0 baseline, 20 checkpoint fault-injection recoveries, failed-tool and approval demonstrations, full trace correlation proof |
| 1. Read-only analytics | Partial | TLC source, two certified metrics, catalog, read-only SQL firewall, query plan, budget controls, immutable artifacts, Supervisor role ledger, validated table/chart/claims, 60-case deterministic regression | Expand to 5-10 metrics, natural-language ambiguity clarification, real-model success rate, 15-result human review, P95 under 60 seconds |
| 2. Data engineering | Not started | Data Engineer role specification only | Second connector, profiling, schema diff, quality rules, transformation proposal, impact analysis, isolated parallel merge, optional approved development write |
| 3. Data science | Not started | Data Scientist role specification only | Python sandbox, snapshot and experiment contracts, reproducible baseline training, leakage/split checks, evaluation report and model card |
| 4. Production readiness | Foundation only | Actor context, tenant/project fields, risk levels, audit sink, fail-closed policy | SSO and row/column policy propagation, short-lived credentials, HITL, DLP, durable audit separation, red-team suite, SLO/alerts/runbooks |
| 5. Business loop | Not started | No external side-effect tools enabled | Per-action approval, idempotency, compensation, kill switch, scheduling, notifications, feedback and cost optimization |

## Current branch scope

`feat/stage1-trusted-reporting` addresses one Stage 1 gap without expanding data access:

- `TableSpec`, `ChartSpec`, `GroundedClaim`, and `DeliveryPackage` are strict v1 contracts.
- Chart instructions reference the immutable query artifact instead of copying data.
- Every delivered claim cites evidence created by the same task.
- The validation gate blocks missing chart artifacts and ungrounded claims.
- The frozen 60-case TLC baseline is executable and produces case-level results.

## Recommended sequence after this branch

1. Close Stage 0 resilience gaps: durable checkpoint resume and fault injection.
2. Finish Stage 1 semantics: natural-language metric resolution, ambiguity responses, and 5-10
   certified metrics.
3. Qualify a faster tool-calling model and run the real-model and human-review acceptance gates.
4. Start Stage 2 with a read-only second connector and deterministic profiling/schema-diff tools.

Stage 2 should not be declared active until the remaining Stage 0 blockers and Stage 1 security
and grounding blockers pass. Performance can remain an explicit exception only in a development
environment, never as an unreported production waiver.
