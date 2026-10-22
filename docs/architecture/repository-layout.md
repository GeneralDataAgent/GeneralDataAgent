# Repository layout and ownership

The original roadmap's suggested layout is retained here as a design reference. It is not an assertion that every directory exists. The [phase roadmap](../../../docs/planning/development-roadmap.md) owns implementation milestones.

```text
src/
  api/                 # Sessions, tasks, approvals, streaming
  orchestration/       # LangGraph state, routing, recovery
  agents/              # Supervisor and specialist definitions
  tools/               # SQL, catalog, Python, dbt adapters
  contracts/           # Versioned Pydantic / JSON schemas
  policy/              # Policy clients, risk classes, obligations
  validation/          # Deterministic quality gates
  artifacts/           # Artifact storage and lineage indexes
  observability/       # Traces, metrics, logs, audit
skills/                # Reviewed domain Skills
evals/
  datasets/            # Versioned task cohorts
  graders/             # Deterministic and human graders
  reports/             # No sensitive raw results in Git
tests/
  unit/
  integration/
  workflow/
  security/
infra/
  sandbox/
  deployment/
docs/                  # English architecture, ADRs, usage, operations
../docs/               # Workspace documentation index
  planning/            # Roadmaps, phase plans, acceptance criteria
  progress/            # Implementation inventories, backlogs, development history
  research/            # Dataset selection and reference sources
  validation/          # Run outcomes, independent review, machine-readable evidence
```

Keep plans, pending work, run outcomes, and benchmark evidence in workspace-level `../docs/`. Keep architecture contracts, actual ADRs, usage instructions, and operations references in repository-level `docs/`, in English. Separate mixed documents by content; link between the authoritative sections instead of maintaining duplicated plans or scores. Large raw datasets, `.artifacts/`, credentials, and runtime state stay under their existing protected/ignored locations.
