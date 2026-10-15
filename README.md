# GeneralDataAgent

[![CI](https://github.com/GeneralDataAgent/GeneralDataAgent/actions/workflows/ci.yml/badge.svg)](https://github.com/GeneralDataAgent/GeneralDataAgent/actions/workflows/ci.yml)

GeneralDataAgent is a governed multi-agent foundation for data analytics, data engineering, and data science. The current implementation includes a governed certified-metric path, a real LLM loaded from CC Switch, and a reproducible NYC TLC open-data validation pipeline.

## Current scope

- Versioned task, artifact, validation, policy, and audit contracts.
- Deterministic policy engine with explicit tool risk levels.
- SQL read-only guard based on parsed syntax trees.
- Local artifact store with path isolation and content hashes.
- Append-only local audit sink for development.
- LangGraph phase-one workflow skeleton with checkpoint support.
- Certified metric catalog with eight TLC metrics and a parameterized query compiler.
- Authorization-filtered English and Chinese metric alias resolution with explicit ambiguity.
- Read-only SQLite execution with timeout, row limit, and query-plan capture.
- Deep Agents specialist definitions for discovery, engineering, analysis, science, verification, and reporting.
- Governed metric Supervisor with actor-bound tools, deterministic role ledger, and a fixed
  post-validation Reporter node.
- Versioned table, chart, claim, and delivery contracts with claim-level evidence validation.
- Anthropic-compatible and OpenAI Responses-compatible LangChain model loading from CC Switch by
  stable provider ID without copying credentials.
- Append-only human-review collection and acceptance summaries.
- Real NYC TLC Parquet ingestion with source hashes and a curated SQLite snapshot.
- Unified read-only SQLite and DuckDB connectors with immutable capability declarations.
- Connector-level SQLite and DuckDB query deadlines with normalized retryable timeout failures.
- Cooperative cancellation propagated through governed profiling into active connector queries.
- Deterministic relation profiling, schema-drift detection, and constrained quality checks.
- Dry-run transformation proposals and deterministic downstream lineage impact analysis.
- Isolated parallel profiling with ordered results, explicit partial failures, and queued-work
  cancellation.
- Optional pinned dbt compile adapter restricted to explicit development and CI targets.
- Hashed, size-limited dbt manifest ingestion for source-to-Dashboard lineage impact.
- Field-level profile comparison with configurable numeric tolerance and a 99% default gate.
- Policy-gated data-engineering runtime with immutable schema, profile, diff, and quality artifacts.
- FastAPI health, capability, contract-validation, and certified-metric endpoints.

Production writes, arbitrary shell execution, model deployment, external delivery, and committed credentials are intentionally excluded from this milestone.

## Development

Python 3.12 and `uv` are used because the current Deep Agents SDK requires Python 3.11 or later.

```bash
uv sync
uv run pytest
uv run ruff check .
uv run uvicorn generaldataagent.api.app:app --reload
```

Then open `http://127.0.0.1:8000/health`.

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

See [the dataset selection](docs/research/open-dataset-selection.md) and
[the acceptance record](docs/validation/tlc-llm-acceptance.md) for scope and limitations.

## Run the real multi-agent validation

```bash
uv run python examples/tlc_multi_agent_demo.py
# Or select a CC Switch Codex/OpenAI Responses provider explicitly:
uv run python examples/tlc_multi_agent_demo.py --app-type codex --provider-id <provider-id>
```

The Supervisor dynamically delegates semantic discovery, certified metric execution, and
independent verification. The control layer then invokes a fixed Reporter node that can only read a
validated delivery package. The run fails closed if the required role order is violated. With the
currently selected free model this path is functionally testable but does not yet meet the Stage 1
latency target; see [the multi-agent acceptance record](docs/validation/tlc-multi-agent-acceptance.md).

## Run the Stage 1 regression baseline

```bash
uv run python evals/run_stage1.py
```

The frozen baseline contains 60 execution cases plus 26 semantic-resolution cases. The runner
executes them against the curated TLC snapshot and emits case-level pass reports. This
deterministic baseline does not replace the still-open real-model, value-level SQL, collected
human-review, and latency acceptance gates.

The API also exposes `POST /v1/analysis/questions` for deterministic natural-language metric
resolution and analysis, `POST /v1/reviews` for appending a scored review, and
`GET /v1/reviews/{task_id}/summary` for evaluating the review gate. Runtime construction must
explicitly configure the metric runtime and review store; the module-level development app keeps
both disabled by default.

See [implementation status](docs/implementation-status.md) for completed and remaining roadmap work.

## Run the Stage 2 data-engineering foundation demo

```bash
uv run python examples/stage2_data_engineering_demo.py
```

The demo creates isolated SQLite and DuckDB sources, profiles the DuckDB relation, proposes
evidence-bound quality rules, executes a not-null rule, compares the two schemas, proposes a
read-only transformation, analyzes downstream impact, and persists every result as a local
immutable artifact. It does not register any write tool. See the
[Stage 2 validation record](docs/validation/stage2-foundation.md) for the current acceptance boundary.

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

When real-environment evidence is available, place one `<gate_id>.manifest.json` plus its referenced
evidence file in a dedicated directory and run:

```bash
uv run python evals/run_stage2_acceptance.py --external-evidence-dir /path/to/evidence
```

The loader verifies exact gate IDs, evidence hashes, relative paths, and validity windows. Missing
evidence remains `not_run`; expired evidence cannot pass; malformed or tampered bundles fail closed.
The default command exits on local-gate status; when an evidence directory is supplied, it exits
successfully only when both local and all external gates pass.

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

```text
src/generaldataagent/
  contracts/       Versioned boundary models
  catalog/         Certified metric definitions and resolution
  policy/          Risk classification and authorization decisions
  tools/           Tool gateway and SQL safety checks
  artifacts/       Content-addressed task artifacts
  orchestration/   LangGraph state and workflow skeleton
  agents/          Deep Agents role definitions and factory
  connectors/      Read-only source adapters and normalized failures
  data_engineering/Profiling, quality, diff, proposals, impact, and governed runtime
  datasets/        Reproducible open-data ingestion
  llm/             Local provider resolution and model adapters
  observability/   Audit event sink
  api/             FastAPI application
```

The parent project documentation in `../docs/` remains the architecture and acceptance source of truth.
