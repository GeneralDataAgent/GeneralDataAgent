# System documentation

This directory contains English documentation for system architecture, design decisions, usage, and operations.

## Architecture and design

- [Research findings and design principles](architecture/research-and-principles.md)
- [System architecture](architecture/system-architecture.md)
- [Multi-agent collaboration](architecture/multi-agent-design.md)
- [Data contracts and evaluation architecture](architecture/data-contracts-and-evaluation.md)
- [Governance and security](architecture/governance-and-security.md)
- [Repository layout and document ownership](architecture/repository-layout.md)
- [Benchmark evaluation protocol](architecture/benchmark-evaluation-protocol.md)
- [TLC reproducibility contract](architecture/tlc-reproducibility-contract.md)
- [Architecture decision records](adr)

## Usage and operations

- [Project usage guide](guides/usage.md)
- [Controlled statistics and modeling](guides/statistics-and-modeling.md)
- [Deployment and operations reference](operations/deployment-and-operations.md)
- [Controlled pilot runbook](operations/controlled-pilot-runbook.md)
- [Business-loop runbook](operations/business-loop-runbook.md)
- [Benchmark evaluation runbook](operations/benchmark-evaluation-runbook.md)
- [Acceptance report format](operations/acceptance-report-format.md)

## Naming and maintenance

Use lowercase English kebab-case filenames with `.md` for authored documents. Use `README.md`
for directory indexes and `NNNN-topic.md` for ADRs. Name internal files by capability or purpose,
without `stageN` prefixes, including after ADR numbers.

Keep documentation in English and organize it around architecture, design contracts, ADRs,
usage, and reusable operating procedures.
