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
  datasets/        Reproducible open-data ingestion
  llm/             Local provider resolution and model adapters
  observability/   Audit event sink
  api/             FastAPI application
```

The parent project documentation in `../docs/` remains the architecture and acceptance source of truth.
