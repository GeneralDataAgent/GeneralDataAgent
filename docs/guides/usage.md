# Usage guide

Run commands from the `GeneralDataAgent/` repository root. The examples use local or explicitly configured services. For implementation progress, historical results, and unclosed acceptance gates, see the [external records](../../../docs/validation/README.md).

## Development

Python 3.12 and `uv` are used because the current Deep Agents SDK requires Python 3.11 or later.

```bash
uv sync
uv run pytest
uv run ruff check .
uv run uvicorn generaldataagent.api.app:app --reload
```

Then open `http://127.0.0.1:8000/health`.

## Check what a deployment actually supports

The development module-level app wires every optional runtime to `None`, so its feature routes answer
`503` and only `/health`, `/v1/capabilities` and `/v1/contracts/task-plan/validate` respond. Ask the
running service instead of guessing:

```bash
curl http://127.0.0.1:8000/v1/capabilities
```

The response lists the six specialists, which runtimes are configured, and the controls in effect
(`production_writes` and `arbitrary_shell` are always `false`). The full as-built capability list,
including items that need external services, is in the [capability catalog](../architecture/capability-catalog.md).

For the architecture behind these routes, read the [as-built architecture](../architecture/as-built-architecture.md)
first; the [system architecture](../architecture/system-architecture.md) is the design target and
differs where the code has not caught up.

## Run the synthetic metric demo

The demo creates a local SQLite database with synthetic order data. It never connects to an external data source.

```bash
uv run python examples/metric_demo.py
```

In another terminal, run:

```bash
curl -X POST http://127.0.0.1:8000/v1/analysis/metrics \
  -H 'content-type: application/json' \
  -H 'x-user-id: demo-user' \
  -H 'x-tenant-id: demo-tenant' \
  -H 'x-project-id: demo-project' \
  -H 'x-data-domain: commerce' \
  -d '{
    "metric_name": "order_count",
    "metric_version": "v1",
    "dimensions": ["channel"],
    "start_time": "2026-10-01T00:00:00Z",
    "end_time": "2026-11-01T00:00:00Z"
  }'
```

## Recompute a rule task after source updates

Use the explicit new-task recomputation path in the [rule recomputation guide](rule-recomputation.md).
Ordinary resume still refuses changed sources. Rule delivery responses include a separate
`rules.deliver` receipt; rule Supervisor results retain execution and delivery receipts in
`receipts`, without changing the `DeliveryPackage` schema.

Run the deterministic rule pipeline (no model) and the rule Supervisor (real model) separately:

```bash
uv run python evals/run_stage10_rule_pipeline.py --output .artifacts/rule-pipeline
uv run python evals/run_stage10_rule_supervisor.py --model EXPLICIT_MODEL_ID \
  --output .artifacts/rule-supervisor
```

The pipeline result is a parameter effect, not a capability gain, and cannot be spliced onto a
model run. The Supervisor follows the same four-role order as the metric path.

## Record research evaluation receipts

Fresh benchmark runs can opt in to persisted step receipts using an explicit research
actor. See the [evaluation receipt guide](evaluation-receipts.md) for identity, readback,
legacy compatibility and qualification requirements, and
[ADR 0010](../adr/0010-execution-receipt-contract.md) for the contract and its weaker verification
levels.

```bash
uv run python evals/verify_evaluation_receipts.py --report .artifacts/run/report.json \
  --output .artifacts/run/receipts.json
```

Receipts record `producer_self_check`, `authoritative_rederivation`, `independent_implementation`,
or `human_attestation`; treat a self-check as provenance, not as verification.

## Route a task and reuse reviewed experience

Both switches live on `evals/run_stage6.py` and are off by default, so an existing paired baseline
keeps one fixed profile. Each switch is part of the protocol fingerprint, so a run with routing on
cannot be averaged with a run that had it off.

```bash
uv run python evals/run_stage6.py --benchmark dabstep --evaluation-protocol stage9-v1 \
  --tool-profile sql-rules --routing --output .artifacts/run-routed/report.json

uv run python evals/run_stage6.py --benchmark bird --limit 60 \
  --experience-database path/to/experience.sqlite \
  --experience-actor '{"user_id":"...","tenant_id":"...","project_id":"...","data_domains":["..."]}' \
  --output .artifacts/run-experience/report.json
```

`--routing` records one lane decision per case; `--tool-profile` and `--routing` cannot be combined
in one run. Experience hints are retrieved only from records that passed independent review; the
store is preflighted read-only before the worker starts, and a store failure drops the hint without
changing the case's outcome. Inspect a store without touching it:

```bash
uv run python evals/audit_stage10_experience.py --store path/to/experience.sqlite \
  --output .artifacts/experience-audit.json
```

Dataset-specific vocabulary and manuals belong in a versioned Skill or knowledge input, not in core
code. See [Dataset extensions](dataset-extensions.md) for the admission path and the worked
`examples/skills/*.json` manifests.

## Run the real-data and real-model validation

Prerequisites:

- CC Switch has a current Anthropic-compatible provider.
- Its API remains managed by CC Switch; no key is copied into this repository.
- Internet access is available for the first source download and model request.

```bash
uv run python examples/tlc_real_data_demo.py
```

This downloads the official January 2025 NYC TLC green-taxi Parquet file and taxi-zone lookup,
records their SHA-256 hashes, creates `data/tlc/tlc-2025-01.sqlite`, executes a certified read-only
metric, and asks the selected model for an evidence-bounded structured narrative. The `data/`
directory, model credentials, local artifacts, and audit state are ignored by Git.

See [the dataset selection](../../../docs/research/open-dataset-selection.md) and
[the acceptance record](../../../docs/validation/tlc-llm-acceptance.md) for scope and limitations.

## Run the real multi-agent validation

```bash
uv run python examples/tlc_multi_agent_demo.py
# Or select a CC Switch Codex/OpenAI Responses provider explicitly:
uv run python examples/tlc_multi_agent_demo.py --app-type codex --provider-id <provider-id>
```

The Supervisor dynamically delegates semantic discovery, certified metric execution, and
independent verification. The control layer then invokes a fixed Reporter node that can only read a
validated delivery package. The run fails closed if the required role order is violated.
Model-specific quality and latency observations belong in [the multi-agent acceptance record](../../../docs/validation/tlc-multi-agent-acceptance.md).

## Run the certified-metric regression baseline

```bash
uv run python evals/run_stage1.py
```

The frozen baseline contains 60 execution cases plus 26 semantic-resolution cases. The runner
executes them against the curated TLC snapshot and emits case-level pass reports. This
deterministic baseline must be evaluated separately from real-model behavior, value-level SQL,
collected human review, and latency; see the [Stage 1 record](../../../docs/validation/stage1-trusted-reporting.md).

The API also exposes `POST /v1/analysis/questions` for deterministic natural-language metric
resolution and analysis, `POST /v1/reviews` for appending a scored review, and
`GET /v1/reviews/{task_id}/summary` for evaluating the review gate. Runtime construction must
explicitly configure the metric runtime and review store; the module-level development app keeps
both disabled by default.

Stage 2 production admission requires five real-environment evidence bundles, independent
signatures, and acceptance through the durable evidence ledger. See [implementation status](../../../docs/progress/implementation-status.md) for the recorded outcome.

## Run the data-science workbench

```bash
uv run --frozen python examples/stage3_data_science_demo.py
uv run --frozen python evals/run_stage3.py
uv run --frozen python evals/run_stage3_acceptance.py
uv run --frozen python evals/run_stage3_oci.py --image-ref NAME@sha256:DIGEST
uv run --frozen python evals/run_stage3_sandbox_probe.py --image-ref NAME@sha256:DIGEST
```

The demo creates an immutable synthetic snapshot and runs the fixed majority-classification
template. The frozen gate covers 20 cases across classification, regression,
random, temporal, and group splits. Unsafe inputs fail before experiment execution. See the
[Stage 3 validation record](../../../docs/validation/stage3-data-science-workbench.md) and
[sandbox decision](../adr/0005-fixed-template-data-science-sandbox.md).

The local process sandbox provides development isolation. The OCI path requires a digest-pinned
image and independent hardening review for production admission. Model deployment is unavailable.
Use the aggregate acceptance runner to verify a content-hashed hardened-sandbox review signed by
at least two distinct trusted reviewers; see the [validation record](../../../docs/validation/stage3-data-science-workbench.md).

When a workbench is explicitly configured, `POST /v1/science/experiments` runs an existing immutable
snapshot. The endpoint is disabled by default, accepts no code or command, and requires the caller's
tenant, project, and data domain to match the snapshot's immutable scope.

### Statistics and modeling tools

The statistical tooling is usable without the workbench: deterministic tests, regression, and
formula-bound analysis run as ordinary tools inside the policy gateway.

```bash
uv run python examples/statistics_modeling_demo.py
uv run python evals/run_stage8_acceptance.py --include-sklearn --output .artifacts/stage8
```

See [Controlled statistics and modeling](statistics-and-modeling.md) for the method rules (sample vs
population standard deviation, effect sizes, assumption checks) and the Stage 8 gates.

## Run the data-engineering foundation demo

```bash
uv run python examples/stage2_data_engineering_demo.py
```

The demo creates isolated SQLite and DuckDB sources, profiles the DuckDB relation, proposes
evidence-bound quality rules, executes a not-null rule, compares the two schemas, proposes a
read-only transformation, analyzes downstream impact, and persists every result as a local
immutable artifact. It does not register any write tool. See the
[Stage 2 validation record](../../../docs/validation/stage2-foundation.md) for the current acceptance boundary.

For execution paths that depend on a frozen schema, `require_compatible_schema` allows additive
columns but raises a structured `SchemaDriftError` for removals, type changes, or nullability
changes. The governed runtime persists the breaking diff before stopping so an orchestrator can
replan without silently using stale fields.

Run the frozen 25-case Stage 2 component baseline with:

```bash
uv run python evals/run_stage2.py
```

Run the reproducible 100-request local profiling load gate with:

```bash
uv run python evals/run_stage2_load.py
```

Run the 50-group cross-source profiling isolation gate with:

```bash
uv run python evals/run_stage2_isolation.py
```

Cross-check 20 deterministic profiles between SQLite and DuckDB with:

```bash
uv run python evals/run_stage2_cross_engine.py
```

Run the frozen 20-case quality-rule matrix with:

```bash
uv run python evals/run_stage2_quality.py
```

Run the 12-check conservative quality-rule proposal gate with:

```bash
uv run python evals/run_stage2_quality_proposals.py
```

Run the frozen 20-case impact-analysis matrix with:

```bash
uv run python evals/run_stage2_impact.py
```

Run the frozen 20-case schema-diff matrix with:

```bash
uv run python evals/run_stage2_schema.py
```

Run the frozen 20-case transformation-safety matrix with:

```bash
uv run python evals/run_stage2_transformation.py
```

Run the 16-check local fake-dbt adapter qualification with:

```bash
uv run python evals/run_stage2_dbt_adapter.py
```

Run the 42-check approval-gated SQLite development-write pilot with:

```bash
uv run python evals/run_stage2_development_write.py
```

Run the frozen 20-case dbt-manifest lineage matrix with:

```bash
uv run python evals/run_stage2_lineage.py
```

Aggregate all local Stage 2 evidence while retaining external blockers with:

```bash
uv run python evals/run_stage2_acceptance.py
```

When real-environment evidence is available, place one `<gate_id>.manifest.json`, its referenced
evidence file, and the required independently signed
`<gate_id>.<key_id>.attestation.json` files in a dedicated directory. Then run:

```bash
uv run python evals/run_stage2_acceptance.py \
  --external-evidence-dir /path/to/evidence \
  --trusted-verifiers /path/to/trusted-verifiers.json \
  --evidence-ledger /durable/path/stage2-evidence.sqlite
```

Sign each generated manifest with an Ed25519 private key held outside the evidence bundle:

```bash
uv run python evals/attest_external_evidence.py \
  --manifest /path/to/evidence/real_dbt_project.manifest.json \
  --private-key /secure/path/release-reviewer.pem \
  --key-id release-reviewer-2026 \
  --verifier release-reviewer
```

The trusted-verifier registry contains the corresponding raw Ed25519 public key as `base64:...`,
the verifier identity and key ID, the exact external gate IDs that key may sign, `valid_from`,
`valid_until`, and an optional `revoked_at`. Optional per-gate policies set `minimum_verifiers`; the
default is one for backward compatibility. Thresholds count distinct verifier identities, not keys,
and the manifest's declared verifier must be among the signers. This permits security and domain
reviewers to jointly authorize higher-risk gates while preventing one reviewer from satisfying a
threshold with rotated or duplicate keys.

The signed statement binds the gate, verifier, key ID, manifest hash, algorithm, and signing time. A
signature must have been created during the key's validity window and after the evidence observation.
Rotation does not invalidate still-current evidence signed while the old key was valid, but a
revocation takes effect deterministically at the acceptance check time. The loader also rejects
future-dated signatures and evidence signed after its own expiry.

The loader verifies identity, gate authorization, the signed statement, evidence hashes, relative
paths, and validity windows. Missing evidence remains `not_run`; expired, unsigned, untrusted,
unauthorized, under-threshold, duplicate-signer, revoked, malformed, or tampered bundles fail closed.
When the complete bundle passes, the acceptance runner atomically records each gate's observation
time and signed manifest hash in the durable SQLite evidence ledger. Re-running the identical bundle
is idempotent; an older observation or a different manifest at the same observation time is rejected
as rollback or equivocation. Every accepted bundle also appends a deterministic audit entry chained
to the previous entry hash. Before any acceptance, the runner verifies the full chain and requires
the latest chained bundle to match the current gate snapshot, so edits to either history or current
state fail closed. Failed, incomplete, or exact-replay bundles never append a new audit entry.

The default command exits on local-gate status; when an evidence directory is supplied, it requires
both the trust registry and ledger and exits successfully only when local and all external gates pass.

Generate the `real_dbt_project` evidence pair from an actual non-production project with:

```bash
uv run python evals/qualify_real_dbt_project.py \
  --project-dir /path/to/dbt-project \
  --profiles-dir /path/to/profiles \
  --target ci \
  --selector model.orders \
  --output-dir /path/to/evidence \
  --source-system dbt-core-ci \
  --verifier release-reviewer
```

The qualifier refuses production environments and production target names, runs compile plus the
selected tests, redacts local paths, and emits both passing and failing evidence without overwriting
an existing evidence pair.

Generate the `authoritative_profiler` evidence pair from two independently produced profile JSON
files with:

```bash
uv run python evals/qualify_authoritative_profiler.py \
  --actual-profile /path/to/generaldataagent-profile.json \
  --authoritative-profile /path/to/authoritative-profile.json \
  --actual-source-system generaldataagent \
  --authoritative-source-system enterprise-profiler \
  --environment staging \
  --output-dir /path/to/evidence \
  --verifier data-quality-reviewer
```

The default agreement threshold is 99%. The evidence contains input hashes and field-level
comparison results, not local file paths, and refuses identical source-system identities.

Generate the `authoritative_catalog` evidence pair from a complete dbt lineage snapshot and an
independent catalog export with:

```bash
uv run python evals/qualify_authoritative_catalog.py \
  --actual-snapshot /path/to/dbt-lineage.json \
  --authoritative-export /path/to/catalog-lineage.json \
  --actual-source-system dbt-manifest \
  --critical-asset source.shop.orders \
  --environment staging \
  --output-dir /path/to/evidence \
  --verifier catalog-reviewer
```

The comparison checks each critical asset's type, direct dependencies, and exact downstream
Dashboard paths. Both graphs must be complete, self-contained, and independently sourced.

Generate the `external_approval_service` evidence pair from an exported service transcript with:

```bash
uv run python evals/qualify_external_approval_service.py \
  --transcript /path/to/approval-transcript.json \
  --output-dir /path/to/evidence \
  --verifier security-reviewer
```

The strict transcript records request and tenant binding, an independent approver, rejection of
self-approval and changed requests, one-time consumption, replay blocking, expiry, and ordered event
timestamps. The emitted evidence omits the tenant identifier and local file paths.

Generate the `production_scale_load` evidence pair from a staging or production load-harness
transcript with:

```bash
uv run python evals/qualify_production_scale_load.py \
  --transcript /path/to/production-load-transcript.json \
  --output-dir /path/to/evidence \
  --verifier performance-reviewer
```

The qualifier requires at least 1,000 requests by default and checks target throughput, bounded
errors and cancellations, artifact completeness, P95 latency, an observed saturation signal,
activated backpressure, and a successful bounded recovery probe. It only validates supplied
staging or production measurements; the local load gate below does not create this external claim.

The gate fails on any failed or cancelled request, missing task artifact, or local end-to-end P95
above five seconds. It is a development regression, not a production capacity claim.
The cross-engine gate requires exact agreement across 380 profile fields; it checks connector parity,
not agreement with an external authoritative profiler.
The quality matrix covers exact violation counts and report outcomes for not-null, uniqueness,
accepted-value, range, and warning-severity behavior.
The proposal gate ensures schema constraints remain errors while snapshot-inferred uniqueness and
ranges remain warnings, and rejects mismatched evidence.
The impact matrix checks exact downstream types, distances, and paths across chains, forks, fan-in,
multiple changed assets, disconnected components, cycles, metrics, and Dashboards.
The schema matrix requires exact change type, ordering, and breaking status across additions,
removals, renames, type/nullability changes, compound drift, and ordering/case stability.
The transformation matrix contains eight accepted read proposals and twelve rejected cases covering
unauthorized relations, mutations, DDL, multiple statements, locking, and incomplete safeguards.
The local dbt gate checks timeouts, nonzero exits, target and selector restrictions, command/output
path redaction, and one-time approval behavior. It does not satisfy the real-project dbt gate.
The development-write pilot is absent by default and can only materialize a reparsed SELECT into a
new `dev_` table in one pinned SQLite development/CI database after exact one-time R2 approval. It
executes the proposal tests in the same transaction and rolls back on failure. It cannot select a
production environment, bind a different database than the registered read connector, overwrite an
object, or enable production writes. Failed approved attempts persist immutable failure evidence.
Durable local approvals remain executable across one runtime restart and non-replayable thereafter.
Source drift after approval is classified, rolled back, persisted, and cannot reuse the grant.
Long-running writes are interrupted at the configured deadline with the same fail-closed guarantees.
Concurrent approvals for one target still create it exactly once and preserve isolated evidence.
Materialized results also have a configurable row budget (100,000 by default); excess rows roll back.
The lineage matrix checks supported node normalization, dependency filtering, incomplete snapshots,
deterministic re-ingestion, and fail-closed malformed-manifest handling. It does not replace an
authoritative catalog comparison.
The aggregate report exits successfully when local development gates pass, but deliberately reports
`stage_acceptance_complete: false` until every real-environment gate has evidence.

The optional dbt adapter also exposes selected `dbt test` only through an R2 approval grant. The
grant is bound to the exact request and tenant, expires, rejects self-approval, and is consumed once.
An optional SQLite approval store preserves pending and consumed state across local process restarts;
reviewers can reject pending requests, requesters can cancel unconsumed actions, and terminal states
survive restart. The default runtime has no approval store or dbt runner configured, so this path
stays disabled.

## Package map

The full map with responsibilities and entry points is in the
[as-built architecture](../architecture/as-built-architecture.md#5-package-map). In short:

```text
src/generaldataagent/
  contracts/        Frozen boundary models (extra="forbid")
  catalog/          Certified metrics and deterministic NL resolution
  policy/           Static rules, exact approvals, budgets, resume
  tools/            ToolGateway, SQL guard, statistics and rule tools
  agents/           Supervisor factory, specialist specs, role ordering
  orchestration/    LangGraph workflows, checkpoints, metric runtime
  artifacts/        Content-addressed and governed stores
  connectors/       Read-only SQLite / DuckDB / PostgreSQL adapters
  data_engineering/ Profiling, quality, diff, proposals, impact, dbt
  data_science/     Snapshots, experiments, sandbox, tracking, model cards
  datasets/         Reproducible open-data ingestion
  business/         Outbox, approvals, schedules, receivers, rules, admission
  review/           Runtime human-review stores
  security/         OIDC, sessions, RLS adapter, DLP, release admission
  observability/    Audit sinks, SLO, alerts, maintenance
  llm/              CC Switch / Magpie adapters, routing, deadlines
  evaluation/       Benchmark harnesses, receipts, comparison, routing
  services/         Approval authority, audit collector, report worker
  api/              FastAPI assembly and the two consoles
```

System architecture and operations are documented in the [internal documentation index](../README.md). Planning and acceptance records are maintained in the [workspace documentation](../../../docs/README.md).

## Controlled production runtime

The Stage 4 runtime provides scoped OIDC/PKCE login and user permissions, durable approvals/reviews,
short-lived PostgreSQL native RLS/column-ACL enforcement, governed text artifacts with JSON/Unicode
DLP and retention, encrypted checkpoints with concurrent-resume fencing, independent signed audit
collection and recovery verification, task budgets, background retries/cleanup, SLO/alerts, and live signed release
admission. These are repository controls; production acceptance requires independent real-environment
evidence. See [implementation/validation](../../../docs/validation/stage4-production-readiness.md) and the
[rollout/recovery runbook](../operations/controlled-pilot-runbook.md).

Run the local baseline with `uv run --frozen python evals/run_stage4_acceptance.py`. Add
`--require-production` in a release pipeline to fail while signed independent admission evidence
is missing or invalid. Deploy through the released factory, never the development module-level app.

For the real PostgreSQL adapter's 19 synthetic isolation checks, use
`uv run --frozen python evals/run_stage4_postgres.py` after obtaining the pinned official image as
documented in the runbook. Add `--pytest` to run the full coverage suite against a fresh disposable
database. CI runs the pytest integration against its Postgres service on Python 3.11, 3.12 and
3.13; the standalone runner above is not itself a CI step.


## Business-loop pilot

The optional authenticated business API supports structured Dashboard/ticket/scheduling-PR/model
registration/IM/report drafts through a durable local outbox. Exact approvals, idempotency,
compensation, independent switches/rate limits and per-occurrence report approvals are included.
The Chinese console at `/business` supports scoped browsing, cancellation, approved execution,
receipt review, read-only recovery and separately approved compensation proposals.
A separate approval authority serves the reviewer console at `/review`; the business process uses a
verified HTTPS client without grant authority. Real reviewer/identity/audit enrollment is required.
Feedback curation and capability admission remain separate trusted review workflows. An opt-in
async model executor adds budgeted routing and encrypted short-lived scoped caching.

External delivery is disabled by default. An explicitly configured Feishu notification receiver
supports exact-approved sending, readback, durable receiver hints and read-only reconciliation.
A separate Feishu report adapter publishes bounded frozen report content as an approved IM message;
cloud-document/file publication is not implemented. A separately configured GitHub receiver prepares
an exact single-file JSON schedule change, creates an approved draft PR and verifies its contents.
GitHub Issues review queues also accept Dashboard drafts, tickets and model-registration requests;
these are human-review artifacts without query execution or model deployment.
Lost acknowledgements quarantine the action for read-only reconciliation; a separately approved
compensation can close only the original unchanged, unmerged draft PR. Actual GitHub target
enrollment and live qualification remain open.
A durable background worker can poll report schedules with freshly issued scoped sessions and
restart-safe leases. A pinned service-JWT adapter validates mounted workload credentials and issues
revocable sessions constrained to the assigned schedule; actual provider enrollment and deployment
remain operator integrations.
Certified report refresh and model routing/cache are integrated into the analysis path, and
new Agent/Skill/connector assembly enforces pinned admission. An opt-in native model router also
meters Supervisor and specialist turns under a shared durable budget with current admission checks.
Production mode still rejects the local business/feedback pilot. Separate qualified runtime variants
provide conditional single-host assembly with remote approvals, signed independent audit/state and
live release/per-destination qualification; real operator enrollment and evidence are required.
A report worker process entry point and disabled Linux service templates support reviewed deployment.
See the [runbook](../operations/business-loop-runbook.md) and
[development/acceptance evidence](../../../docs/validation/stage5-business-loop.md). Run the local matrix with
`uv run --frozen python evals/run_stage5_acceptance.py`; missing independent external evidence keeps
aggregate production acceptance incomplete.

### Business API and consoles

The business router is mounted only when `create_app(..., business_actions=...)` receives a runtime;
the console is served from the same app at `/business`, and the reviewer console at `/review` comes
from a separate approval-authority process. Endpoints (`GET`/`POST /v1/business/drafts`,
`/drafts/{id}/execute|reconcile|cancel`, `/schedules`, `/schedules/{id}/tick|cancel`) all require a
session whose scope matches the draft.

Run the worker as a process when schedules are in use. It loads an operator-owned factory rather
than taking connection details on the command line:

```bash
uv run python -m generaldataagent.services.report_worker --factory myorg.worker:build --check-config
uv run python -m generaldataagent.services.report_worker --factory myorg.worker:build --once
```

Service templates live in `deploy/systemd/` and ship disabled.


## Public benchmark evaluation

Both CC Switch and an existing Magpie gateway are supported without changing desktop settings.
Use `--app-type magpie --provider-id magpie --model EXPLICIT_MODEL_ID`; see the
[Magpie runbook](../operations/benchmark-evaluation-runbook.md#magpie-gateway) for calibration and dataset commands.

The evaluator uses pinned BIRD Mini-Dev, InfiAgent-DABench and DABstep inputs, answer-blind bounded
SQL/statistical tools, case-level evidence, model-free replay, paired comparisons, and explicit
release gates. Select one cohort, model, and resource policy per run; see the
[evaluation runbook](../operations/benchmark-evaluation-runbook.md) for preparation and reproduction.
Historical scores and qualification decisions are maintained in the [external validation index](../../../docs/validation/README.md).

### Treat one run as one observation

A single run pair is not evidence of a gain. The tools are arranged so the claim has to be earned:

```bash
# One arm, one protocol, one cohort.
uv run python evals/run_stage6.py --benchmark bird --limit 60 \
  --evaluation-protocol stage9-v1 --tool-profile sql-rules \
  --output .artifacts/arm-a/report.json

# Single run vs single run: reported, but blocked as evidence (single_run_arm).
uv run python evals/compare_stage6.py --baseline .artifacts/arm-a/report.json \
  --candidate .artifacts/arm-b/report.json --output .artifacts/pair.json

# Three repeats per arm: paired by task, task-clustered bootstrap, exact sign test.
uv run python evals/compare_repeated_runs.py \
  --baseline .artifacts/arm-a/run-1.json --baseline .artifacts/arm-a/run-2.json \
  --baseline .artifacts/arm-a/run-3.json \
  --candidate .artifacts/arm-b/run-1.json --candidate .artifacts/arm-b/run-2.json \
  --candidate .artifacts/arm-b/run-3.json \
  --cohort-status development --output .artifacts/repeats.json
```

`compare_repeated_runs` resamples tasks rather than task-runs, refuses a single-run arm, a copied
report, an arm whose outcomes flip beyond the stability limit, a cross-arm cohort or fingerprint
change, and an unmeasured cost. Its output carries `design_blockers` and `criteria_met`;
`production_accepted` is always `false`, and `--cohort-status held_out` is refused unless every run
records the same frozen cohort file hash.

### Freeze an unseen cohort before running it

Choosing a cohort after seeing scores makes every result post-hoc. Declare it first:

```bash
uv run python evals/freeze_cohort.py --benchmark bird --limit 45 --seed 20261028 \
  --report-dir .artifacts --output evals/cohorts/bird-unseen-45.json

uv run python evals/run_stage6.py --benchmark bird --limit 45 \
  --cohort-file evals/cohorts/bird-unseen-45.json --output .artifacts/arm-a/run-1.json
```

The declaration collects every case ID that prior reports already ran, samples only the remainder,
and writes IDs, seed, report hashes and a `cohort_hash` — never question text or answers. The runner
enforces the file: missing or duplicate IDs, a changed task, or a hash mismatch fails the run instead
of falling back to sampling. Running the cohort consumes it.

Verify a run's step receipts with:

```bash
uv run python evals/verify_evaluation_receipts.py --report .artifacts/arm-a/run-1.json \
  --output .artifacts/arm-a/receipts.json
```

