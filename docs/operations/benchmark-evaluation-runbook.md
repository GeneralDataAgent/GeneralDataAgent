# Benchmark evaluation runbook

Run commands from the implementation repository root with the frozen uv environment. Raw datasets and generated artifacts remain outside Git by default. This guide documents procedures; phase plans, historical results, and acceptance progress live in the [external documentation](../../../docs/validation/README.md).

## Data and scorer qualification

Prepare pinned datasets and qualify the scorer before model runs. Initial downloads are approximately 87 MB for task/table sources and 800 MB for the BIRD archive, expanding to roughly 1.5 GB of SQLite files. Preparation downloads/extracts data without executing upstream code. `.artifacts/stage6/bird-environment.json` records reference-SQL environment checks, not agent performance.

```bash
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/prepare_stage6.py
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/prepare_stage6_bird.py
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/qualify_stage6_bird.py
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen pytest tests/test_stage6_evaluation.py tests/test_stage6_release_gates.py
```

## Real-model evaluation

Use an existing CC Switch provider with `--app-type claude` or `--app-type codex`; an omitted provider ID uses the corresponding current configuration. Do not copy credentials. Every output directory must be new; separate model configurations use separate runs.

Workers are independent question processes, not cooperating specialist roles. Worker count enters the protocol fingerprint. Respect service pacing; SDK failures are not automatically retried. The case process deadline is 125 seconds. Current evaluation defaults impose neither a cumulative token cap nor a per-call output cap; consumption remains fully recorded. An explicitly requested cumulative cap is checked after a response and is not a provider-side billing guarantee.

The original `sql` profile provides controlled SQL only. DABstep documents/JSON are data, not authority to expand tools. Legacy CSV import converts finite numeric columns to REAL and empty fields to NULL, which can change leading zeros, large integers, or missing-value semantics; use `identifier-v2` for the declared preservation policy. Modeling tasks do not install dependencies or execute arbitrary Python.

Each run writes `report.partial.json` and, when complete, `report.json` with case results, protocol and source fingerprints. Partial runs are not complete scores. Preserve failures; never combine best cases across protocols/models. Source byte hashes identify actual code; a starting commit alone does not prove a clean frozen run.

```bash
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/run_stage6.py --benchmark bird --limit 60 --workers 2 --app-type codex --provider-id PROVIDER_ID --bird-databases data/stage6/bird/databases --output .artifacts/stage6/bird-run
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/run_stage6.py --benchmark infi --limit 60 --workers 2 --app-type codex --provider-id PROVIDER_ID --output .artifacts/stage6/infi-run
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/run_stage6.py --benchmark dabstep --limit 10 --workers 1 --app-type codex --provider-id PROVIDER_ID --output .artifacts/stage6/dabstep-run
```

## Aggregation and regression

Exit 1 means release diagnostics found blockers; results are still saved. A first run without matching baseline, independent review, repetition, or online evidence is expected to fail admission. Supply complete same-track reports with repeated `--baseline PATH` options. Mismatched cohorts/protocols, altered summaries, duplicate cases, or invalid telemetry are rejected.

Use Stage 1–5 runners and `uv run --frozen pytest --cov=generaldataagent --cov-fail-under=85` for product regression. Network benchmark runs are separate from ordinary CI; CI validates graders, isolation, and gates. Select model and resource policy explicitly for formal runs.

```bash
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/run_stage6_acceptance.py --report .artifacts/stage6/bird-run/report.json --report .artifacts/stage6/infi-run/report.json --report .artifacts/stage6/dabstep-run/report.json --output .artifacts/stage6/acceptance.json
```

## Offline replay without model calls

Replace the report path for InfiAgent or DABstep. Replay reconstructs the cohort, verifies source/database digests, and rescores stored predictions without model requests. Reproduced scoring is not independent semantic correctness or production acceptance.

```bash
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/replay_stage6.py --report .artifacts/stage6/bird-run/report.json --output .artifacts/stage6/bird-replay.json
```

## Explicit model and tool selection

Verify available model IDs against the configured service when selecting a route. `--model EXACT_MODEL_ID --transport openai-chat` affects this evaluation only, without editing current CC Switch configuration. Parent and worker reconstruct/check route hashes; configuration drift, floating selectors such as `~...latest`, and credential-bearing URL parameters are rejected.

`sql` is the original baseline. `sql-analysis` adds fixed describe, Pearson, descriptive univariate regression, and document retrieval on scoped existing tables/columns. No arbitrary code or additional sources are granted. A descriptive fit is not held-out predictive performance. Retrieval returns at most 6,000 characters with source hashes.

Scoring, import, SQL execution, resources, and environment belong in the protocol; model/prompt/tool implementations belong in the system fingerprint. Changed data/scoring/resources prevent pairing. Trusted provider `usage.cost` is accumulated as `provider_reported`; unknown or failed-call billing remains null. The existing USD 0.02 observed-cost guard is checked after calls, not a provider-side hard billing limit.

## Service pacing, circuit breakers, and loop termination

`--request-interval-seconds 1.6` coordinates processes through `.state/stage6/request-pacing.sqlite`, storing only target hashes and timestamps, never credentials/questions/answers. Select model/request settings for each run. Native NVIDIA Nemotron Chat supports `--thinking off --json-mode` as described in the [NIM interface](https://build.nvidia.com/playbooks/nim-llm/instructions); this controls the current request.

HTTP 401/402/403/404 immediately opens the service circuit; three consecutive other model-service failures also open it. In-flight work may finish under existing deadlines. Later cases remain in the denominator with `service_circuit_open` and `attempted=false`. Reports separate attempted counts/P95 so skipped zero-duration cases cannot appear fast. Exit 2 denotes service blocking; exit 1 denotes release diagnostics. The runner never purchases credits or changes account defaults.

Invalid action fields receive structured repair feedback. Three identical actions terminate as `repeated_action`; explicit `final_sql` may finish an explored query. Database context contains one bounded sample row; full values require actual queries.

## Cross-tool checks and repeated experiments

Tool qualification covers 52 task-referenced CSVs, up to three numeric columns each, with independent DuckDB comparison. Repetition requires independent records, identical ordered tasks, data digests, protocol, and system. Copied reports cannot count as independent runs. Excess service failures preserve observed score changes but block algorithm attribution and significance claims.

```bash
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/qualify_stage6_analysis.py
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/compare_stage6.py --baseline BASELINE_REPORT --candidate CANDIDATE_REPORT --output COMPARISON_JSON
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/qualify_stage6_stability.py --report RUN1 --report RUN2 --report RUN3 --output STABILITY_JSON
```

## Identifier-preserving import and counterfactual scorer checks

`--csv-importer identifier-v2` preserves whole columns as TEXT when they contain leading-zero integers, very large integers, or nonnumeric text; other finite numbers become REAL. `legacy-v1` exists for historical reproduction. Import changes alter the protocol even with identical raw files, so old and new runs cannot be paired.

Use fresh directories and real calls for each of three repeats; aggregation accepts multiple `--repeat-report PATH` arguments. Complete system/data/protocol identity is required. Counterfactual scorer qualification uses synthetic SQLite cases for accidental equality, NULLs, duplicates, and date boundaries; it is not model performance.

```bash
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/qualify_stage6_analysis.py --csv-importer identifier-v2 --include-dabstep
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/qualify_stage6_scorer.py
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/run_stage6.py --benchmark dabstep --limit 10 --workers 1 --app-type claude --provider-id PROVIDER_ID --model nvidia/nemotron-3.5-lightning-30b-a3b --transport openai-chat --thinking off --json-mode --tool-profile sql-analysis --csv-importer identifier-v2 --request-interval-seconds 1.6 --output .artifacts/stage6/dabstep-fresh-run
```

## Product Supervisor integration

Prepare `data/tlc/tlc-2025-01.sqlite` first. The driver invokes the actual product Supervisor in one asynchronous session, with 60 seconds per question, local evidence, and comparison to certified deterministic metrics. Output files must not already exist. Native tools, rather than JSON actions, are used; arbitrary code and external sends remain unavailable.

A call that never reaches the executor cannot demonstrate request binding. Success requires the correct request, complete role order, matching results, and hash-verified real evidence. Three TLC probes do not qualify public benchmark assets or production admission.

For multi-Python regression, explicitly select interpreter and isolated environment so uv does not recreate the default project environment.

```bash
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/qualify_stage6_supervisor.py --provider-id PROVIDER_ID --model nvidia/nemotron-3.5-lightning-30b-a3b --limit 3 --output .artifacts/stage6/supervisor-fresh-run/report.json
```

```bash
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache COVERAGE_FILE=.artifacts/stage6/py312.coverage uv run --frozen --python 3.12 pytest --cov=generaldataagent --cov-fail-under=85
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache UV_PROJECT_ENVIRONMENT=/tmp/gda-stage6-py311 COVERAGE_FILE=.artifacts/stage6/py311.coverage uv run --frozen --python 3.11 pytest --cov=generaldataagent --cov-fail-under=85
```

## Token policy and service preflight

Current defaults for `--max-tokens` and `--max-output-tokens` are unset: there is no evaluator cumulative or per-call output token ceiling. Do not pass these options for uncapped evaluation. Provider context/output limits still apply. Finite limits remain optional for controlled comparisons and historical reproduction; per-call values must be at least 32. Explicit `--max-tokens 24000 --max-output-tokens 3000` records the old limits on the current runner, but exact historical replay additionally requires its pinned source/protocol. Both limits enter pairing fingerprints; output limits also enter the effective route.

Shared pacing accepts 0–60 seconds and still consumes case time. Different limits, pacing, versions, or import semantics are not directly pairable.

Run a six-case real-context calibration before a full batch and pass `--preflight-report`. Calibration requires at least six complete attempted cases, observed replies for all cases, service failures at most 5%, and complete token telemetry. It does not select by answer accuracy. Benchmark, protocol, route, and source snapshot must match. An invalid preflight writes a rejection with exit 2 before workers or model calls start. Passing calibration does not guarantee later capacity, answer quality, or production readiness.

`sql-precision` supplies declared primary/foreign-key context only on SQL-output tracks. Reproduce read-only schema qualification with `evals/qualify_stage6_sql_schema.py --output PATH`. Historical calibration failures and results remain in the [external iteration summary](../../../docs/validation/stage6-iteration-summary-2026-10-20.md). The commands below intentionally illustrate finite per-call calibration; use identical explicit options in both arms.

```bash
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/run_stage6.py --benchmark bird --limit 6 --workers 1 --app-type claude --provider-id PROVIDER_ID --model nvidia/nemotron-3-super-120b-a12b --transport openai-chat --thinking off --json-mode --max-output-tokens 1024 --request-interval-seconds 16 --csv-importer identifier-v2 --tool-profile sql-precision --bird-databases data/stage6/bird/databases --output .artifacts/stage6/calibration-fresh
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/run_stage6.py --benchmark bird --limit 60 --workers 1 --app-type claude --provider-id PROVIDER_ID --model nvidia/nemotron-3-super-120b-a12b --transport openai-chat --thinking off --json-mode --max-output-tokens 1024 --request-interval-seconds 16 --csv-importer identifier-v2 --tool-profile sql-precision --bird-databases data/stage6/bird/databases --preflight-report .artifacts/stage6/calibration-fresh/report.json --output .artifacts/stage6/full-fresh
```

## Magpie gateway

The evaluation runner and metric-Supervisor qualifier support `--app-type magpie`. Read only the explicitly configured `model_providers.magpie` entry in `~/.codex/config.toml`: `base_url`, `wire_api`, `experimental_bearer_token`, and `http_headers`. Do not edit Codex/CC Switch/Magpie settings or copy upstream provider secrets.

Always specify `--model`. The global Codex model may belong to a different provider. Query the configured gateway's `/v1/models` when choosing an available pinned path, such as the previously observed `workbuddy/deepseek-v4.1-flash`; avoid floating `auto/` routing in controlled comparisons. A gateway model name does not independently attest server weight revisions. Cross-model changes are not evidence for a single algorithm improvement.

Without `--transport openai-chat`, use the configured Responses/Chat protocol. JSON mode remains Chat-only. Unwrap authentication headers only at request time; record header/adapter hashes rather than secrets. Parent and worker verify the route again. Operators can explicitly use `MagpieProvider.load(config_path=...)` for another read-only configuration; the current CLI uses the existing Codex configuration path.

The narrative adapter uses function-call structured output for Magpie and validates the same Narrative schema. Route-selected metadata overrides untrusted model self-identification. Small successful calls and JSON-mode settings cannot guarantee valid real-task actions. Retain invalid outputs, failures, deadlines, and the full denominator. Service readiness is separate from answer quality and production admission. The finite output limits below are explicit examples, not defaults.

```bash
# Exercise the existing structured narrative entry point through Magpie.
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python examples/cc_switch_smoke.py --app-type magpie --provider-id magpie --model workbuddy/deepseek-v4.1-flash --timeout-seconds 20

# Calibrate on real task context; require complete service readiness.
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/run_stage6.py --benchmark infi --limit 6 --workers 1 --app-type magpie --provider-id magpie --model workbuddy/deepseek-v4.1-flash --transport openai-chat --json-mode --max-output-tokens 3000 --tool-profile sql-repair --csv-importer identifier-v2 --request-interval-seconds 3.5 --output .artifacts/stage6/magpie-infi-calibration

# Run the fixed 60-case cohort after matching service preflight.
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/run_stage6.py --benchmark infi --limit 60 --workers 1 --app-type magpie --provider-id magpie --model workbuddy/deepseek-v4.1-flash --transport openai-chat --json-mode --max-output-tokens 3000 --tool-profile sql-repair --csv-importer identifier-v2 --request-interval-seconds 3.5 --preflight-report .artifacts/stage6/magpie-infi-calibration/report.json --output .artifacts/stage6/magpie-infi-full

# Qualify actual product metric orchestration, not public benchmark performance.
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/qualify_stage6_supervisor.py --app-type magpie --provider-id magpie --model workbuddy/deepseek-v4.1-flash --limit 3 --output .artifacts/stage6/magpie-supervisor/report.json
```

## Historical v7 reproduction

The following examples came from the historical v7 development record. Use the matching pinned source to reproduce that run; current defaults and protocol hashes differ. All commands run from the implementation repository and use fresh output paths. The report builder verifies summary digests, paired protocols, and stored prediction scoring without calling a model. Its manifest contains report locations, not credentials or answer labels. Historical results remain [outside the runbook](../../../docs/progress/stage6-iteration-v7.md).

```bash
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/run_stage6.py --benchmark infi --limit 60 --workers 1 --app-type claude --provider-id PROVIDER_ID --model nvidia/nemotron-3-super-120b-a12b --transport openai-chat --thinking off --json-mode --request-interval-seconds 3.5 --csv-importer identifier-v2 --tool-profile sql-repair --output .artifacts/stage6/infi-repair-fresh
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/qualify_stage6_supervisor.py --provider-id PROVIDER_ID --model nvidia/nemotron-3-super-120b-a12b --bound-tools --output .artifacts/stage6/supervisor-bound-fresh/report.json
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/qualify_stage6_repair.py --report .artifacts/stage6/dabstep-nvidia-enhanced-v5/report.json --output .artifacts/stage6/feedback-replay-fresh.json
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/build_stage6_iteration_report.py --manifest evals/stage6_iteration_v7.json --output ../docs/validation/stage6-iteration-v7-results
```
