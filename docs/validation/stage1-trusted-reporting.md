# Stage 1 trusted reporting acceptance record

## Run

- Date: 2026-10-13
- Branch: `feat/stage1-trusted-reporting`
- Datasets: `evals/datasets/stage1.jsonl` and `evals/datasets/stage1-semantic.jsonl`
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

| Semantic resolution | Cases | Passed |
|---|---:|---:|
| Resolved aliases | 16 | 16 |
| Ambiguous aliases | 6 | 6 |
| Not found or unauthorized | 4 | 4 |
| **Total** | **26** | **26** |

Combined runtime: 0.82 seconds on the local acceptance environment. This is an offline deterministic
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
- Eight certified metrics cover count, fare, charge, distance, and passenger aggregations.
- Exact English and Chinese aliases resolve to one certified metric; intentionally shared aliases
  return multiple candidates and a clarification question.
- Unauthorized-domain metrics are filtered before search results are constructed.
- The append-only review store rejects a second review from the same reviewer and computes the
  documented review gate from distinct reviewers.
- The complete automated suite passes 71 tests with 89% line coverage.

## Boundary

This increment does not claim full Stage 1 acceptance. The metric-count and ambiguity-handling
implementation requirements are complete, and the human-review collection mechanism exists.
Real-model task success, value-level SQL accuracy, reviews of at least 15 successful results, and
P95 latency remain open evidence gates. The complete gap list is maintained in
`docs/implementation-status.md`.
