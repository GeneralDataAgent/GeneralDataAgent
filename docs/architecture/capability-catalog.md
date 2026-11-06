# Capability catalog

What the repository can do today, and why each entry is or is not on by default. This page is
derived from the code, not from the roadmap; planned-but-absent items are listed at the end.

Status vocabulary:

- **Available** — the path exists and is exercised by deterministic tests in CI.
- **Opt-in** — implemented but disabled until an assembly or flag enables it.
- **Adapter** — code exists, but a real external service or enrollment is required and not included.
- **Absent** — deliberately not implemented.

## 1. Certified analytics

| Capability | Entry point | Status |
|---|---|---|
| Certified metric query over an approved relation | `POST /v1/analysis/metrics`, `MetricAnalysisRuntime.run` | Opt-in (needs a metric runtime) |
| Natural-language metric resolution with explicit ambiguity | `POST /v1/analysis/questions`, `catalog/resolver.py` | Opt-in |
| Fixed five-node workflow `discover → plan → analyze → validate → deliver` | `orchestration/workflow.py` | Available |
| Read-only SQL guard (single statement, no mutation/locking, relation allowlist) | `tools/sql_guard.py` | Available |
| Immutable, content-addressed artifacts; governed store with DLP, retention, holds | `artifacts/` | Available |
| `DeliveryPackage` with evidence-bound claims and charts | `contracts/`, `_deliver` node | Available |
| Append-only human review with duplicate-reviewer rejection and pass summary | `POST /v1/reviews`, `GET /v1/reviews/{task_id}/summary` | Opt-in |
| Checkpointed resume after interruption | `POST /v1/analysis/tasks/{thread_id}/resume` | Opt-in |
| Creator-scoped progress journal and cursor replay for the metric graph | `GET /v1/analysis/tasks/{task_id}/events`, [guide](../guides/task-events.md) | Opt-in local pilot |
| Durable metric task acceptance, metadata, retry and fenced worker | `POST /v1/analysis/tasks`, `services/metric_task_worker.py`, [guide](../guides/task-submission.md) | Opt-in local pilot; no production qualification |
| Creator-authorized cancellation with pending/confirmed stop and offline v2 cutover | `POST /v1/analysis/tasks/{task_id}/cancel`, [guide](../guides/task-cancellation.md) | Opt-in local pilot; no orphan acknowledgement or provider kill |
| Private bounded CSV upload, explicit text confirmation and evidence-rechecked native profiling | `/v1/analysis/files/csv`, [guide](../guides/private-csv-intake.md) | Local opt-in; no numeric/Excel conversion, sharing or production qualification |
| Creator-private CSV metadata discovery with bounded snapshot cursors | `GET /v1/analysis/files/csv`, [guide](../guides/private-csv-intake.md#discovery-after-refresh) | Current exact scope; no uploaded text, expiry cleanup, artifact integrity claim or production qualification |
| Read-only retained/expired CSV inventory and reviewable plan | `generaldataagent-csv-maintenance`, [guide](../guides/private-csv-intake.md#read-only-maintenance-inventory) | Explicit local store/key/index; no deletion, capacity release, hold changes or production qualification |
| One-time-approved removal of reviewed expired CSV sources with content-free tombstones | `generaldataagent-csv-cleanup`, [guide](../guides/private-csv-intake.md#reviewed-physical-cleanup), [ADR](../adr/0022-reviewed-expired-csv-source-removal.md) | Local operator CLI; no safe erasure, evidence deletion, hold release or production qualification |
| Owner-scoped reviewed batches for inactive display history | `services/preference_maintenance.py`, [guide](../guides/preference-history-maintenance.md) | Local CLI dry-run by default; preserves current revision, no secure erasure |
| Explicit private display preference CAS/history and evidence-bound result presentation | `/v1/analysis/preferences/display`, `/v1/analysis/tasks/{task_id}/presentation`, [guide](../guides/confirmed-display-preferences.md) | Opt-in local pilot; no free-form memory or execution-context injection |
| Exact-domain-view task pages and evidence-rechecked completed results | `GET /v1/analysis/tasks`, `GET /v1/analysis/tasks/{task_id}/result`, [guide](../guides/task-results.md) | Opt-in local pilot; creator-private, no execution on result reads |
| Bounded task status/progress SSE with live authorization and reconnect cursors | `GET /v1/analysis/tasks/{task_id}/stream`, [guide](../guides/task-streaming.md) | Opt-in local pilot; no raw graph/model stream |
| Versioned task evidence rubric with named deterministic verifiers and three fixed business templates | `review/rubric.py`, `review/rubric_templates.py`, [guide](../guides/evidence-rubrics.md), [ADR 0023](../adr/0023-versioned-evidence-rubrics.md), [ADR 0024](../adr/0024-business-rubric-templates.md) | Deterministic library port; no metric resolution, model judgement, automatic revision or production qualification |
| Capability self-description | `GET /v1/capabilities` | Available (always on) |

## 2. Open analysis and benchmark evaluation

The evaluation harness is a separate system from the product API: it runs a `BoundedSQLAgent` per
case in an isolated subprocess, with its own step, time, and token budgets.

| Capability | Entry point | Status |
|---|---|---|
| Three-track benchmark runs (BIRD mini-dev, InfiAgent-DABench, DABstep dev) | `evals/run_stage6.py` | Available (needs a model route) |
| Nine fixed tool profiles from bare SQL to rule execution | `--tool-profile {sql, sql-analysis, sql-repair, sql-precision, sql-semantic, sql-semantic-plan, sql-semantic-json, sql-science, sql-rules}` | Available |
| Per-task lane routing with step/second/repair budgets | `--routing`, `evaluation/routing.py` | Opt-in, **unvalidated by paired evaluation** |
| Six-class failure attribution (capability gap and ambiguity do not consume repair budget) | `evaluation/routing.py::failure_cause` | Available |
| Research step receipts with an explicit actor | `--receipt-actor`, `evals/verify_evaluation_receipts.py` | Opt-in |
| Execution receipts with a declared verification level (`producer_self_check` … `human_attestation`) | `contracts/receipt.py`, [ADR 0010](../adr/0010-execution-receipt-contract.md) | Available |
| Versioned receipt reader: an unstated or unsupported `receipt_version` is refused by name on every read path | `contracts/receipt.py::load_receipt` | Available |
| Paired comparison of two single runs | `evals/compare_stage6.py` | Available |
| Within-arm stability (flip rate, duplicate-report rejection) | `evaluation/stage_six_stability.py` | Available |
| Task-clustered paired repeats with bootstrap interval and exact sign test | `evals/compare_repeated_runs.py` | Available |
| Minimum detectable effect and honest cost degradation for a recorded cohort | `repeat_comparison.minimum_detectable_effect`, `cost` block | Available |
| Power/task-count estimate from observed discordance | `repeat_comparison.tasks_for_power` | Available |
| Frozen unseen cohort, enforced at run time | `evals/freeze_cohort.py`, `run_stage6.py --cohort-file` | Available (one 45-case BIRD cohort frozen, not yet run) |
| Frozen cohort file re-verified against the stored runs (benchmark, aggregate hash, file bytes) | `freeze_cohort.verify_cohort_file` | Available |
| Reviewed experience hints injected into the repair loop | `--experience-database/--experience-actor` | Opt-in, off by default |
| Offline experience extraction and read-only audit | `evals/extract_stage10_experience.py`, `evals/audit_stage10_experience.py` | Available (candidates only; never auto-approved) |

## 3. Data engineering

| Capability | Entry point | Status |
|---|---|---|
| Read-only SQLite and DuckDB connectors with normalized errors and deadlines | `connectors/` | Available |
| Deterministic profiling, schema diff with breaking-change classification | `data_engineering/` | Available |
| Evidence-bound quality-rule proposal and execution | `data_engineering/` | Available |
| Read-only transformation proposals with tests, impact, rollback | `data_engineering/` | Available |
| Lineage ingestion and shortest-path impact analysis | `data_engineering/`, `GET` via runtime | Available |
| Pinned non-production `dbt compile`, selected `dbt test` behind an R2 approval | `data_engineering/dbt*` | Adapter |
| R2-approved SQLite `dev_`-table write pilot (single transaction, row budget, rollback) | `evals/run_stage2_development_write.py` | Opt-in, absent by default |

## 4. Data science and statistics

| Capability | Entry point | Status |
|---|---|---|
| Immutable dataset snapshots with explicit column roles | `data_science/snapshots` | Available |
| Strict experiment contracts and leakage/split/metric gates | `POST /v1/science/experiments` | Opt-in |
| Fixed-template sandbox (repository-owned worker, no arbitrary code) | `data_science/sandbox.py` | Available |
| Digest-pinned OCI sandbox (pull-never, no network, read-only root, capability drop) | `evals/run_stage3_oci.py` | Adapter (needs a local Docker/Podman) |
| Classification/regression baselines, EDA, subgroup errors, model cards | `DataScienceWorkbench` | Available |
| Append-only experiment tracking | `SQLiteExperimentTracker` | Available |
| Mirror to an external experiment index: local record first, explicit `pending` on failure | `MirroringExperimentTracker` | Adapter (needs an admitted external index) |
| Workload scope binding one service run to an actor, task, snapshot and domain set | `security/science_scope.py`, `ExecutionComposition.workload_scope` | Available |
| Model registry **draft only**, exact R2 approval, `deployment_approved=false` | `data_science/registration.py` | Available; deployment **Absent** |
| Statistical tests and formula-bound analysis tools (Pearson, IQR, public formulas) | `tools/statistical_methods.py`, `tools/public_formulas.py` | Available |

## 5. Business loop, rules, and governance

| Capability | Entry point | Status |
|---|---|---|
| Durable draft outbox with exact approvals, idempotency, expiry, cancellation | `business/actions.py` | Available |
| Business console, `/business` | `api/business_ui.py` | Opt-in (requires business runtime) |
| Reviewer console, `/review` | `services/approval_service.py` (separate app) | Opt-in |
| Report schedules and restart-safe worker | `business/schedules.py`, `business/worker.py`, `services/report_worker.py` | Opt-in |
| Feishu notification and frozen-report IM delivery with readback/reconciliation | `business/feishu.py` | Adapter |
| Cloud-document publication with look-before-write, readback and an explicit ambiguous outcome | `business/feishu_document.py` | Adapter (no application registered; no consumer wired yet) |
| GitHub scheduling draft PR and Issues review queues | `business/github.py` | Adapter |
| Document rule model, Decimal computation, applicability semantics | `business/rules/` | Available |
| Rule delivery as a `DeliveryPackage` with per-artifact authorization | `rules.deliver`, `RuleDeliveryRuntime` | Available |
| Atomic rollback of a partially written delivery: reverse discard, `rule_delivery_rollback_incomplete` | `RuleDeliveryRuntime`, `LocalArtifactStore.discard` | Available |
| Rule supervisor with the same four-role order as metrics | `agents/rule_supervisor.py` | Available (live-model reliability measured, not assumed) |
| Feedback curation, capability admission (skill/agent/connector) | `business/governance.py`, `business/extensions.py` | Available |

## 6. Security and production readiness

| Capability | Entry point | Status |
|---|---|---|
| OIDC/PKCE login, short-lived opaque sessions, revocation | `security/identity.py`, `security/login.py` | Available |
| Scoped query executor with native RLS/column-ACL adapter | `security/source.py`, `connectors/postgres_native.py` | Adapter (needs PostgreSQL) |
| JSON/Unicode DLP scan before governed artifact writes | `security/dlp.py` | Available |
| Verified, hash-chained audit with independent collector | `observability/verified_audit.py`, `services/audit_collector.py` | Available |
| Task budgets across runtime/tokens/scan/cost/calls | `policy/budgets.py` | Available |
| Ed25519 release admission, two independent signer roles | `security/admission.py`, `security/release_bundle.py` | Available (no signed evidence shipped) |
| Production assembly | `api/production.py::create_released_app` | Available, requires the full control set |
| SLO, critical alerts, retention maintenance | `observability/` | Available |

## 7. Agents and skills

| Capability | Entry point | Status |
|---|---|---|
| Six declared specialists with fixed tool groups | `agents/specs.py` | Available |
| Governed supervisor with fs/shell tools and auto-summarization removed | `agents/factory.py` | Available |
| Code-enforced role ordering for metric and rule supervisors | `agents/supervision.py` | Available |
| Skills with full contracts (budgets, dependencies, positive and negative examples) | `business/extensions.py::SkillDefinition` | Available |
| Skill admission requiring an active capability record and a narrowing tool set | `ExtensionAdmission` | Available |

## 8. What is not supported

- Production writes initiated by an agent: `permission.change` is permanently denied and the only
  write path is the default-off `dev_` pilot.
- Arbitrary code, shell, or filesystem access in the supervisor or the science sandbox.
- Model deployment, promotion, or registry activation beyond a draft.
- Unbounded retries: repair budgets are per task and per failure class.
- Publishing a benchmark gain as a system conclusion without a frozen cohort, at least three paired
  repeats, verified cost, independent review, and shadow validation. The tool refuses; the docs do
  not restate it as caution.
