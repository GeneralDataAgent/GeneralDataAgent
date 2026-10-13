# Stage 1 trusted reporting acceptance record

## Run

- Date: 2026-10-13
- Branch: `feat/stage1-trusted-reporting`
- Dataset: `evals/datasets/stage1.jsonl`
- Data snapshot: NYC TLC January 2025 curated SQLite snapshot
- Model calls: none; this record covers deterministic delivery and policy behavior

## Results

| Category | Cases | Passed |
|---|---:|---:|
| Certified metric | 30 | 30 |
| Uncertified dimension | 10 | 10 |
| Unauthorized domain | 10 | 10 |
| Unknown metric or version | 10 | 10 |
| **Total** | **60** | **60** |

Runtime: 0.95 seconds on the local acceptance environment. This is an offline deterministic
regression duration, not the real-model first-useful-result latency metric.

## Controls verified

- Grouped results receive a deterministic bar-chart specification; scalar results receive a KPI
  specification.
- Table and chart specifications reference the immutable query-result artifact.
- Chart specifications are stored as separate content-addressed artifacts with lineage to query
  data.
- Model-authored summaries, insights, and limitations are represented as individual grounded
  claims and cite both the query result and preserved narrative.
- Deterministic summaries cite the query result.
- Delivery contract validation rejects references not present in the evidence package.
- The workflow validation gate checks chart preservation and claim grounding before delivery.

## Boundary

This increment does not claim full Stage 1 acceptance. Natural-language ambiguity, five-to-ten
metric coverage, real-model task success, human review, and P95 latency remain open. The complete
gap list is maintained in `docs/implementation-status.md`.
