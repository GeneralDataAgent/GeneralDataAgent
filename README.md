# GeneralDataAgent

GeneralDataAgent is a governed multi-agent foundation for data analytics, data engineering, and data science. The first implementation milestone delivers the contracts and control plane required by the project architecture before connecting a real model or production data source.

## Current scope

- Versioned task, artifact, validation, policy, and audit contracts.
- Deterministic policy engine with explicit tool risk levels.
- SQL read-only guard based on parsed syntax trees.
- Local artifact store with path isolation and content hashes.
- Append-only local audit sink for development.
- LangGraph phase-one workflow skeleton with checkpoint support.
- Certified metric catalog and parameterized query compiler.
- Read-only SQLite execution with timeout, row limit, and query-plan capture.
- Deep Agents specialist definitions for discovery, engineering, analysis, science, verification, and reporting.
- FastAPI health, capability, contract-validation, and certified-metric endpoints.

Production writes, arbitrary shell execution, model deployment, external delivery, and real credentials are intentionally excluded from this milestone.

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
  observability/   Audit event sink
  api/             FastAPI application
```

The parent project documentation in `../docs/` remains the architecture and acceptance source of truth.
