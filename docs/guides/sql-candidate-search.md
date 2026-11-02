# Bounded SQL candidate search

`orchestration/sql_search.py` provides an answer-blind pairwise SQL selector and a
whole-task model budget. It adapts diverse synthesis and tournament selection from
[Agentar-Scale-SQL, v6](https://arxiv.org/html/2509.24403v6). It does not include the
paper's trained generator or selector, training examples, or benchmark answers.

`select_sql_candidates` accepts a public question, instructions, authorized schema,
one to three SQL strings, a caller-owned `execute(sql)` port and a model with
`invoke(messages)`. The execution port must enforce source authorization, read-only
SQL, column scope, deadlines and complete bounded results. The selector does not
create authority. Production callers must supply their normal governed execution
port; passing a local SQLite executor is a development example only.

All candidates are executed before selection. Complete result bags retain duplicate
multiplicity, NULLs, types, output width and column order; row order is ignored for
grouping. Exact groups use one representative each. Distinct groups enter an
all-pairs tournament of at most three calls; tied scores retain the earliest
representative. Model responses may only choose 0 or 1 from the presented pair.
Invalid decisions refuse selection. No score or reference SQL enters this API.

Execution equivalence is an optimization, not semantic verification. It does not
prove an ORDER BY, correct business interpretation, or validity on another data
snapshot. Selector previews show at most five rows and 160 characters per text
cell, with explicit incompleteness. Full result hashes are retained separately.

## Development agent adapter

`BoundedSQLAgent(..., tool_profile="sql-semantic-json", max_steps=12,
sql_search="diverse-tournament-v1")` generates three independently prompted
candidates through the existing guarded agent. Each branch has at most
`(max_steps - 3) // 3` model calls, reserving three for selection. This intentionally
trades one deep trajectory for several shorter ones. The complete source/schema
context remains available; there is no benchmark-specific schema pruning.

One `SearchModelBudget` meters generation and selection together, including failed
calls and unknown usage/cost. One `RepairBudget` is shared across branches. Time,
observed tokens, known cost and calls cannot reset per branch. Overshooting
responses are measured and discarded. A service error stops the search even when
an earlier candidate succeeded. The outer isolated process deadline remains active.

The adapter currently supports SQL-output evaluation without document, experience,
Skill or receipt extensions; unsupported assemblies refuse before model execution.
Default behavior stays `sql_search="none"`.

Run both comparison arms from the same revision, against the same prepared inputs:

```bash
uv run python evals/run_stage6.py --benchmark bird \
  --bird-databases data/stage6/bird/databases --limit 60 \
  --evaluation-protocol stage7-v1 --tool-profile sql-semantic-json \
  --max-steps 12 --max-seconds 180 --workers 2 \
  --app-type magpie --provider-id magpie \
  --model workbuddy/deepseek-v4.1-flash --transport openai-chat \
  --request-interval-seconds 3.5 --output .artifacts/bird-single-new
```

For the candidate, add `--sql-search diverse-tournament-v1` and choose a fresh output
path. Resource limits stay in the evaluator protocol; strategy and source hashes
are system fingerprints. Unset cumulative and output-token ceilings stay unset;
provider context limits still apply and unknown fees remain unknown.

The local duplicate-sensitive result comparator is not official BIRD EX. A single
development pair does not establish generalization, significance, trained-selector
reproduction or production admission. Keep regressions, service failures and the
entire planned denominator in the report.
