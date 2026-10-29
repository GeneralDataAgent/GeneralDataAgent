# Repository layout and ownership

This is the actual layout of the repository as of commit `82b39da`, not a proposed one. For what each
Python package does, see the [package map](as-built-architecture.md#5-package-map).

```text
GeneralDataAgent/
  pyproject.toml            # package metadata, ruff/pytest config, one console script
  Makefile                  # sync / test / lint / format / check / api wrappers
  uv.lock                   # pinned dependency resolution
  README.md                 # project entry and quick start
  src/generaldataagent/     # 19 packages, 215 modules
    contracts/              # frozen Pydantic boundary models
    catalog/                # certified metrics and deterministic NL resolution
    policy/                 # static rules, exact approvals, budgets, resume
    tools/                  # ToolGateway, SQL guard/executor, statistics and rule tools
    agents/                 # supervisor factory, specialist specs, role ordering
    orchestration/          # LangGraph workflows, checkpoints, metric runtime
    artifacts/              # content-addressed and governed stores
    connectors/             # read-only SQLite / DuckDB / PostgreSQL adapters
    data_engineering/       # profiling, quality, diff, proposals, impact, dbt
    data_science/           # snapshots, experiments, sandbox, tracking, model cards
    datasets/               # reproducible open-data ingestion (TLC)
    business/               # outbox, approvals, schedules, receivers, admission
      rules/                # document rule model, matching, Decimal computation
    review/                 # runtime human-review stores
    security/               # OIDC, sessions, RLS adapter, DLP, release admission
    observability/          # audit sinks, SLO, alerts, maintenance
    llm/                    # CC Switch / Magpie adapters, routing, deadlines
    evaluation/             # benchmark harnesses, receipts, comparison, routing
    services/               # approval authority, audit collector, report worker
    api/                    # FastAPI assembly, business API and console
  evals/                    # 70 runnable gates, qualifiers, and acceptance runners
    cohorts/                # frozen cohort declarations (IDs and hashes only)
  tests/                    # 192 flat test modules (no conftest, no subdirectories)
  examples/                 # demos and live-model evidence runs
    skills/                 # example skill admission manifests
  infra/sandbox/            # pinned OCI build for the science sandbox
  deploy/systemd/           # disabled service templates for reviewed deployment
  docs/                     # English architecture, ADRs, guides, operations
  data/                     # local, gitignored benchmark and TLC inputs
  .artifacts/               # local, gitignored run outputs and evidence
```

## Ownership rules

- Plans, progress matrices, and run outcomes live in the workspace-level `../docs/` (Chinese).
- Architecture contracts, ADRs, usage, and operations live here (English). Split a mixed document
  instead of duplicating it.
- `data/`, `.artifacts/`, `.state/`, and credentials stay out of Git. Frozen cohort declarations are
  the exception: they contain IDs and hashes only, never question text or answers.
- Benchmark evidence referenced from docs lives under `.artifacts/`; if a claim needs to survive a
  clean checkout, the evidence has to be summarized in a document with its hashes, not copied in.
