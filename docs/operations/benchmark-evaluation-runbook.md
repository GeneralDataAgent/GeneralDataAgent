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

Workers are independent question processes, not cooperating specialist roles. Worker count enters the protocol fingerprint. Respect service pacing; SDK failures are not automatically retried. The case process deadline sits five seconds above the worker's own time budget, so a routed lane
may spend its full budget without the OS killing the process and reporting a service-style
`worker_deadline`. With the default 120-second budget that is 125 seconds; the analysis lane
(150) and governed lane (180) get correspondingly longer deadlines. An explicit deadline at or
below the worker budget is refused, because it would relabel a budget timeout as a service fault.
Explicit process deadlines must be finite integers or floats (booleans are excluded). NaN,
infinities and nonnumeric values raise `invalid process deadline` before a worker starts;
valid fractional deadlines above the task budget are passed through unchanged.
Direct `BoundedSQLAgent` construction also validates its own budgets: `max_steps` must be a
positive integer, and `max_seconds` must be a finite positive integer or float; booleans are
excluded. Invalid types, NaN and infinities raise `ValueError("invalid agent budget")` during
construction. Fractional seconds remain supported for direct callers; the isolated worker and
protocol interfaces retain their integer task-budget requirements.
The isolation boundary requires worker output to be a JSON object with a finite, nonnegative
numeric `elapsed_seconds` (booleans are excluded). Invalid JSON, a different top-level shape,
or missing/invalid timing becomes `worker_error`, with empty answers/trace/hints, zero observed
tokens, unknown usage and null cost. Raw stdout is not copied into that failure record. Valid
worker timing is kept as `agent_elapsed_seconds`, while `elapsed_seconds` measures parent-side
wall time. This checks shape and timing only, not the complete worker result contract.
`request_interval_seconds` must be a finite int or float in `[0, 60]`; booleans are excluded.
Zero means no pacing, and every other accepted value is forwarded unchanged to the worker's
shared pacer. NaN, infinities, negatives, values above 60 and nonnumeric inputs raise
`ValueError("invalid request interval")` before a subprocess starts. Previously NaN and negative
values silently disabled pacing at the worker's `> 0` check, contradicting the declared interval.
The protocol records the deadline that applies to the run: 125 seconds for a fixed profile, and
the largest lane budget plus the margin (185 seconds today) for a routed run, which also records
`hard_case_deadline_mode: per_lane` and the `lane_time_budgets` map so each case's deadline can be
derived from its lane. The protocol's `seconds` field records the task time ceiling: 120 for a
fixed profile and the largest lane budget (180 today) for routing. The protocol constructor
accepts this declared budget explicitly, requires a positive integer and checks that the hard
deadline exceeds it. The lane map still gives each case's actual budget; the top-level ceiling
does not grant every lane 180 seconds. The same rule applies to the other per-case limits: a fixed
profile declares 6 steps and 3 repairs, while a routed run declares the largest lane ceiling
(8 steps, 3 repairs) plus `lane_step_budgets` and `lane_repair_budgets`, and `max_repairs_per_case` records
the ceiling actually granted. All of these enter the protocol hash, so two runs that differ here
cannot be paired as the same evaluator. Current evaluation defaults impose neither a cumulative token cap nor a per-call output cap; consumption remains fully recorded. An explicitly requested cumulative cap is checked after a response and is not a provider-side billing guarantee.

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

## Execution-receipt readback

A run started with `--receipt-actor` stores per-step result artifacts next to its
report. Readback rehashes those stored bytes without a model call and without
rescoring, and records the report hash, per-case receipt counts and any receipt or
Stage 7 qualification errors. It refuses to overwrite an existing output.

```bash
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/verify_evaluation_receipts.py --report .artifacts/stage6/bird-run/report.json --artifact-root .artifacts/stage6/bird-run/artifacts --output .artifacts/stage6/bird-readback.json
```

Legacy reports without receipt metadata still read back, contributing zero
receipts. Readback proves byte identity and binding consistency only; use
`replay_stage6.py` separately for scoring replay, and keep each dataset's
denominator separate.

## Explicit model and tool selection

Verify available model IDs against the configured service when selecting a route. `--model EXACT_MODEL_ID --transport openai-chat` affects this evaluation only, without editing current CC Switch configuration. Parent and worker reconstruct/check route hashes; configuration drift, floating selectors such as `~...latest`, and credential-bearing URL parameters are rejected.

`sql` is the original baseline. `sql-analysis` adds fixed describe, Pearson, descriptive univariate regression, and document retrieval on scoped existing tables/columns. No arbitrary code or additional sources are granted. A descriptive fit is not held-out predictive performance. Retrieval returns at most 6,000 characters with source hashes.

Scoring, import, SQL execution, resources, and environment belong in the protocol; model/prompt/tool implementations belong in the system fingerprint. Changed data/scoring/resources prevent pairing. Trusted provider `usage.cost` is accumulated as `provider_reported`; unknown or failed-call billing remains null. The existing USD 0.02 observed-cost guard is checked after calls, not a provider-side hard billing limit.

## Service pacing, circuit breakers, and loop termination

`--request-interval-seconds 1.6` coordinates processes through `.state/stage6/request-pacing.sqlite`, storing only target hashes and timestamps, never credentials/questions/answers. Select model/request settings for each run. Native NVIDIA Nemotron Chat supports `--thinking off --json-mode` as described in the [NIM interface](https://build.nvidia.com/playbooks/nim-llm/instructions); this controls the current request.

The worker passes its selected `max_seconds` to `PacedModel`. Its pacing deadline is the
construction time plus that budget minus five seconds; slot reservation retains another
five-second margin. The default 120-second case therefore keeps the existing 110-second
initial wait allowance; routed 90/150/180-second cases use 80/140/170 seconds instead of sharing
a fixed 110-second allowance. Tool binding preserves the original deadline rather than
restarting it. These margins limit slot waiting, not provider response duration; the agent and
process deadlines remain separate controls. An exhausted pacing allowance is rejected before
reserving a slot, rather than clamped to zero and allowed through an idle slot. The allowance is
checked again after reservation and any sleep, so SQLite contention or a late wake cannot send
an expired request. The agent records `PacingDeadlineExceeded` as `time_budget`; no provider
request means no additional observed usage. A slot already reserved before expiry is not reclaimed,
which keeps shared pacing conservative. No pacing intervals or retry counts change.

HTTP 401/402/403/404 immediately opens the service circuit; three consecutive other model-service failures also open it. In-flight work may finish under existing deadlines. Later cases remain in the denominator with `service_circuit_open` and `attempted=false`. Reports separate attempted counts/P95 so skipped zero-duration cases cannot appear fast. Exit 2 denotes service blocking; exit 1 denotes release diagnostics. The runner never purchases credits or changes account defaults.

Invalid action fields receive structured repair feedback. Three identical actions terminate as `repeated_action`; explicit `final_sql` may finish an explored query. Database context contains one bounded sample row; full values require actual queries.

## Cross-tool checks and repeated experiments

Tool qualification covers 52 task-referenced CSVs, up to three numeric columns each, with independent DuckDB comparison. Repetition requires independent records, identical ordered tasks, data digests, protocol, and system. Copied reports cannot count as independent runs. Excess service failures preserve observed score changes but block algorithm attribution and significance claims. A `severe_incident` in any supplied repeat blocks stability qualification regardless of the allowed flip or service-error rates. This includes an older repeat when the current report is clean; release diagnostics retain the nested incident blocker and reject repeat stability. All task outcomes, denominators and resource statistics remain unchanged.

Every repeat must also supply positive integer `requested_n` and `population_n` values
(booleans are excluded), with exactly `min(requested_n, population_n)` recorded results.
Missing, malformed or incomplete planned denominators add `incomplete_planned_cohort` and
prevent stability qualification while retaining observed statistics. Matching truncated case
lists across repeats are insufficient. Older reports without these fields remain diagnostic
inputs; do not backfill them from a later run. These declarations are caller-supplied evidence,
not authenticated proof of the originally planned cohort. Every report must explicitly have
`run_status: completed`; any missing, malformed or other status adds `incomplete_run_status`,
even if all planned rows are present. Existing outcomes and statistics remain visible. Older
reports without this status cannot establish completion and must not be relabeled; a completed
status is still a declaration, not authenticated proof that execution finished. Repeated reports
must also agree on `runner_hash` and the top-level `model` declaration, alongside the existing
protocol/system/model-route checks. A mismatch raises `incomparable repeated-run runner_hash`
or `incomparable repeated-run model`, including through release diagnostics. Equality checks
consistency of the supplied provenance; it does not authenticate which runner or model executed.

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

## Rule Supervisor diagnostics

`evals/run_stage10_rule_supervisor.py` measures the four-role team over requests accepted by
its deterministic parser. It is a development diagnostic, not a public benchmark score or
production acceptance. Declare the annex empty-list interpretation explicitly; do not change
that interpretation between paired runs. Parser refusals stay in the full denominator.

```bash
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR="$PWD/.cache/uv" TMPDIR="$PWD/.artifacts/stage10" uv run --frozen python evals/run_stage10_rule_supervisor.py --app-type magpie --provider-id magpie --model workbuddy/deepseek-v4.1-flash --limit 10 --empty-list-is-wildcard --output .artifacts/stage10/rule-supervisor-fresh/report.json
```

Output and its sibling `<report-stem>-runtime` directory must be fresh and inside the
implementation repository. The runtime directory retains `payments.sqlite`, the encrypted
rule checkpoint `state`, and a random 32-byte `checkpoint.key` written with owner-only file
permissions inside an owner-only directory. This local development key is stored alongside
the ciphertext for reproducible diagnosis; it is not production key management. Keep that
directory local and do not include its key in reports or shared evidence. Retention survives
normal exit and exceptions; there is no automatic cleanup or production retention policy.

Each attempted row records the bound request, rule thread ID, Supervisor thread ID and ordered
diagnostic events without model prose or prompts. Each response is reduced to a shape: the
emitting agent (main Supervisor or the specialist that replied), tool names, invalid tool-call
names, text length, finish reason, and the request's `tool_choice` (with the named tool when one was
required). An empty reply, a prose-only reply and a call that failed argument parsing therefore stay
distinguishable without storing the text. A reply that calls nothing although its request required a
tool is classified `required_tool_call_not_honoured` if no later tool call resumes the sequence.
Named requirements also check the requested name; calling a different tool does not satisfy them.
Malformed tool arguments are recorded separately and do not count as an absent required call.
Without an unmet requirement or a higher-priority stop, a final text reply with no tool call is
`response_without_tool_call`; empty and malformed final replies stay
`incomplete_sequence_reason_unknown`. A completed delivery whose role sequence or artifact readback
is not confirmed is `delivery_check_failed`; a completed delivery with all four roles and readable
artifacts that merely disagrees with the published answer is `answer_mismatch`, so a wrong answer is
not filed as a delivery fault. A row whose Supervisor reported a declared deterministic refusal
(`deterministic workflow refusal: <code>` in its limitations) is `workflow_refusal`: the stop has a
recorded cause, so it is not left as reason-unknown. The refusal is only read back from that exact
prefix with a non-empty code; free text that happens to resemble one is not accepted. An observed
model or tool error and an explicit specialist `failed`/`needs_human` report still outrank it.
Every non-completed row keeps
`semantic_root_cause: not_established`. These classifications describe associated callback evidence,
not a verified wire capture or an endpoint root cause. A local payload simulation cannot establish
what an earlier live run sent; old reports without request metadata remain unqualified. A later
parsed tool call suppresses an earlier mismatch as the stop category; this ordered-response rule
does not establish causal recovery across concurrent agents. All generations from one request keep
its callback settings. These events are observations, not a full model conversation or the
Supervisor's in-memory checkpoint, which is not retained. Each case persists atomically; a `running` report with pending cases is incomplete.
Model attempts, responses, errors and finite specialist role/status reports are recorded
separately. Specialist declarations never authorize execution. Missing or failed-call token usage
remains unknown even when known totals are zero. Costs remain null without independent billing
telemetry.

This runner reuses `ServiceCircuitBreaker`: HTTP 401/402/403/404 opens the circuit immediately;
three consecutive other model-service failures also open it. Later parseable cases are
`service_circuit_open`, `attempted=false` and remain in the denominator; parser refusals keep
their own outcome. A service-blocked run saves all cases, sets `blocked_by_service` and exits
2. A gateway `/v1/models` response alone does not establish model-service readiness.

## Authored rule business qualification

`evals/qualify_stage9_business.py` runs the versioned synthetic corpus in
`evals/datasets/stage9-rule-business.json`. It checks exact natural-language request fields
before executing the real checkpointed workflow. Arithmetic expectations come from raw records
and Fraction-based oracles, never workflow output. All four operations and explicit refusal
cases remain in the planned denominator; correctly expected refusals count as qualification
passes, not successful business answers.

Counterfactual verification requires the complete serialized `CounterfactualResult` field set
and current schema version, the requested rule ID, a genuine integer transaction count, and
finite decimal text for all three amounts. Missing or extra fields, a different rule, malformed
amounts and boolean counts fail verification without delivery. Previously completed checkpoints
are not automatically reverified after a code change; use a fresh thread or an explicitly
updated workflow version when qualifying the new verifier.

For `select` and `average_fee`, the computation must contain exactly `fees` and `average_fee`.
Each fee row must contain the complete serialized `FeeResult` fields and current schema
version, match the selected rule's coefficients and source, and carry finite decimal fee text.
The average must also be finite decimal text when rules match. An empty selection remains a
valid workflow result with an empty fee list and null average. Malformed records fail
verification without delivery; they are not converted into successful empty results.

```bash
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR="$PWD/.cache/uv" TMPDIR="$PWD/.artifacts/stage10" uv run --frozen python evals/qualify_stage9_business.py --output .artifacts/stage9/business-fresh
```

Use a fresh output directory inside the implementation repository and an existing temporary
parent. The run retains the original corpus bytes, corpus/parser/oracle/runner fingerprints,
per-case request and answer comparisons, errors, and atomic progress reports. Exit status is
1 if any case fails. Model calls are zero. This is an authored, inspected development corpus;
independent human review and unseen evaluation remain `not_run`. It does not qualify model
delegation or production release admission.

Add `--governed` to execute the same corpus through `GovernedRuleRunner`, using a separate
fresh output directory. Each accepted case checks the task-bound artifact reference, content
hash and size, exact evidence/checkpoint equality, verification status, and the two ordered
policy audit events against expected request fingerprints, actor, tenant, task and trace.
`governance_checked_n` and `governance_passed_n` count these cases separately from parser
refusals. Each case retains its own artifact and audit directory. This mode uses development
local storage and JSONL audit; it does not qualify production source authorization, retention,
release admission, or independent audit. Denial and outage fault injection is covered by
`tests/test_stage9_governed.py` and evidence-tampering checks by
`tests/test_stage9_business_qualification.py`. Evidence writes use the original caller's role,
identity and data domains; the adapter does not substitute the reporter role. A policy allowing
execution but denying that caller's artifact write keeps the completed checkpoint and returns
no evidence reference. Recovery still requires authorization for the caller's `resume` and
`artifact.write` requests.

## Rule evidence recovery qualification

`evals/qualify_stage9_recovery.py` injects an evidence outage after the workflow has
completed, closes and reopens the encrypted checkpoint database, then resumes through the
same development gateway. `--fault audit` (the default) fails the evidence authorization audit;
`--fault write` fails `put_json` after its authorization was audited but before any bytes are
written. Run each mode in its own fresh directory; their ten-case results remain separate. It checks that changed requests and duplicate `run` calls are
rejected without changing the checkpoint, and that `resume` writes task-bound, hash-verified
evidence without replaying any of the five workflow nodes. Answers are checked against the
raw-record Fraction oracles.

```bash
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR="$PWD/.cache/uv" TMPDIR="$PWD/.artifacts/stage10" uv run --frozen python evals/qualify_stage9_recovery.py --output .artifacts/stage9/recovery-fresh
```

The planned denominator is explicitly the ten accepted structured requests from the eighteen
case authored corpus. Parser refusals are not recovery scenarios. The fresh run saves all
cases, initial fault observations, per-node counts, artifacts, audit logs and atomic progress.
Audit `allow` records for rejected duplicate/changed requests mean policy authorization only;
the workflow binding checks still reject execution. An `artifact.write` allow event also
records authorization only; in write-fault mode its handler fails and returns no artifact.
This is database close/reopen in one process. It does not qualify partial-file writes,
process-kill recovery, production audit or unseen evaluation.

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

## Rule-enabled DABstep development evaluation

### Route selection with `--routing`

`--routing` replaces a fixed `--tool-profile` with a per-case lane chosen from public text
before any model call. Lanes are `short` (5 steps, 2 repairs), `analysis` (7/3) and `governed`
(8/3). Markers are matched as whole words, with an optional plural `s`; `coffee`, `feedback`,
`accurate` and `corporate` therefore do not match `fee` or `rate`, and an underscored identifier
such as `fee_rate` is not treated as published prose. The chosen lane, its reasons and a source
hash are written to the run manifest, so a misroute is auditable after the fact. Each case's
immutable `Route` is selected once before asset preparation: both the manifest and worker
profile/step/time/repair arguments use that same decision. Execution does not call the router
again, and an attempted case records the same route in its result. This binds the in-process
selection to execution; it does not make source files or worker dependencies immutable.
A question matching no marker takes `short`, whose smallest budget makes a misroute visible as a budget
failure rather than silent extra spend.

A repair refusal does not always mean its allowance was exhausted. For nonretryable causes,
including policy denial and fixed-source failures (`rule_source_record_invalid`,
`payments_invalid_row`), the case retains the original failure code and the trace records
`repair_not_applicable_<cause>`; no experience hints are requested. These source failures are
classified as `source` and are not retried because model feedback cannot repair read-only input
records. The repair report's
`exhausted` flag (and summary `exhausted_n`) counts refusals caused by the total repair ceiling
or identical-failure ceiling. Other refusals remain in `refused` without setting that flag.
Historical reports retain their original diagnostic fields and must not be silently rewritten.

The system fingerprint includes Stage 7/8/9 sources according to the profiles selected for
this cohort, including when routing replaces the fixed profile. A mixed cohort records the
union of those dependencies. Repair-budget and retry-classification source hashes are also
recorded for fixed-profile runs. These identify the system under evaluation; they do not alter
the scoring protocol or establish model performance. Older manifests with missing dependency
hashes must retain that limitation rather than receive hashes from a later checkout.
For rule profiles, `stage9_sources` includes `business/rules/payments.py`, whose strict date
parser is shared by the direct evaluator. A change to that parser changes the system
fingerprint in both fixed and routed rule runs without changing the scoring protocol or cohort.
The repeated-run gate therefore rejects those runs as different systems; a missing required
source raises instead of silently omitting its hash. This inventory remains partial, not a
complete transitive dependency lock.

### Qualification implementation fingerprints

The synthetic facet/context, business-language and recovery qualification reports now include
`workflow_source_sha256`, captured before execution from an explicit package-source inventory.
It covers the workflow, matcher, payment source, boundary contracts, checkpoint implementation
and SQLite guard. Governed business/recovery runs additionally include the gateway, policy,
approval/budget code, local artifact store and development audit. Synthetic reports also record
the qualification runner hash. Existing parser/oracle and recovery runner fields are retained.
An unreadable required source fails before creating the run output directory.

This inventory is not a full transitive dependency lock, a signature, or proof that files could
not change after hashing. It excludes product delivery, which these qualification runners do
not execute. Preserve missing-fingerprint limitations on historical reports; do not backfill
them from a newer checkout. Matching source hashes do not establish unseen-task generalization,
independent review or production acceptance.

### Protocol

`sql-rules` requires `--evaluation-protocol stage9-v1 --benchmark dabstep`. It adds
`analyze_fee_rules` to the native function surface. The runtime validates the typed request,
reads bounded existing tables in read-only mode, retains complete annex records with a source
hash and exact formula passages in the case trace, and accepts answer bindings only to
computed IDs, average fee, or rate delta. Unspecified facets in `select`/`average_fee` are not
constraints; `applicable`/`counterfactual` derive merchant thresholds from the full natural
month even for a day-scoped transaction set. Null and empty-list wildcard declarations remain
separate and are recorded with the caller's exact document excerpt.

The development rule workflow retains `Decimal` values when passing monthly volume and
fraud amount ratio from its context checkpoint to threshold matching. Converting them to
binary floats can move a value across a strict boundary while the shared verifier repeats
the same error. Regression cases compare both workflow delivery and the direct evaluation
tool against raw-record `Fraction` expectations on both sides of the threshold. This avoids
an extra conversion; it cannot recover precision already lost in source REAL columns or
remove the existing decimal arithmetic context limits.

Monthly fraud aggregation requires an explicit fraud indicator for every included row, even
when the answer targets a different day of that month. Missing fields, nulls and blank strings
raise `ValueError("fraud indicator required")` rather than being counted as non-fraudulent.
Existing explicit true/false, 1/0 and yes/no encodings remain supported. This shared engine
check applies to the workflow, direct rule evaluator and verification helpers. It does not
impute missing labels or retroactively reverify completed checkpoints and historical scores.
The direct rule evaluator also uses the workflow's strict source-date parser, and its module is
included in the qualification workflow-source fingerprint.

For the development SQLite payment source, `year` and `day_of_year` must be Python integers
(excluding booleans) or ASCII digit strings. Floats, including integral floats, and fractional,
signed or whitespace-padded strings are refused with `payments_invalid_row`; dates must also
remain inside the declared calendar year. Source dates are never rounded or truncated into a
different day. A source failure during monthly context leaves a resumable checkpoint, but does
not produce computation, delivery or a completion evidence artifact. A gateway allow event
records authorization to attempt execution, not successful completion. This source check does
not retroactively revalidate completed checkpoints or establish production source admission.

The payment content-hash cache tracks the main database and WAL separately using each file's
device, inode, size, modification time and change time, including WAL absence. A committed WAL
update can reuse its existing allocation without changing the main file; total size alone does
not detect it. A changed content hash rejects resume before another node runs, and a change
observed after a handler prevents its output from entering the checkpoint. SHM is coordination
state and is not part of this content-cache signature. This remains a local metadata-based
change detector: relation reads do not share an atomic snapshot, and concurrent writers or
metadata-preserving replacements are not covered by a production consistency guarantee.

Before checking selection or fees, the workflow verifier rebuilds its full context from
the bound request and authoritative sources. The saved context must match exactly, including
field types, scope, formula, counts and monthly aggregates. A mismatch yields
`rule_context_mismatch` and no delivery; extra fields are also refused. This is a fresh
source re-derivation through the same application code, not an independent semantic oracle.
Existing completed checkpoints are not retroactively reverified; use a fresh run or an
explicitly changed workflow version when qualifying this verifier change.

The workflow's `applicable` computation must contain exactly `items` and `total_count`.
Items must be a list of string rule IDs matching the re-derived selection; the count must be
a Python integer (excluding booleans), equal to the selection size. Selection metadata must
also say `complete is True` and provide a non-boolean integer `total_count`; Python numeric
equality alone is insufficient (`1`, `1.0` and `True` compare equal in some structures).
A selection metadata mismatch stops verification before later computation checks or delivery.
Empty selections with count zero remain valid. Existing completed checkpoints are not
retroactively reverified.

Product packaging accepts a verified `applicable` result with an empty `items` list as a
valid no-match answer, rendered explicitly as `applicable_rules = []`. The cited task
artifact retains the actual empty array and its verification. Missing, null or non-list
`items` remain invalid; an undefined `average_fee` remains a refusal. Packaging still requires
a completed, verified workflow and uses the existing policy, audit and feedback path.

Each product delivery registers one `artifact.write` handler on its own gateway. The result,
chart and claim artifacts each require a separate policy decision using the original caller's
role, actor scope, task and trace. With an audit sink configured, successful packaging emits
four decisions: `rules.deliver` followed by three `artifact.write` requests whose fingerprints
bind the content and lineage. A denied write or audit outage stops packaging before feedback
registration. Earlier artifacts may remain after a later failure; this is not a transactional
rollback across artifacts. Optional audit remains a development configuration and does not
establish production admission.

Use native function calls without `--json-mode`. With the current OpenAI SDK, combining JSON
object response formatting with these non-strict wire tools invokes auto-parsing and raises
`Only strict function tools can be auto-parsed` before any provider request. Runtime Pydantic
validation remains strict independently of provider wire formatting. A failed local invocation
must remain recorded and must not be reported as exhausted service credits.

```bash
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=.cache/uv uv run --frozen python evals/run_stage6.py --benchmark dabstep --limit 10 --workers 1 --app-type magpie --provider-id magpie --model workbuddy/deepseek-v4.1-flash --transport openai-chat --csv-importer identifier-v2 --request-interval-seconds 3.5 --tool-profile sql-rules --evaluation-protocol stage9-v1 --output .artifacts/stage9/dabstep-rules-fresh
```

This evaluator adapter does not establish production `ToolGateway` admission. Development
results are not paired with historical protocols or official leaderboard results. General
rule verifier helpers still require further hardening for arbitrary caller-built results;
the adapter uses loaded rule records and operation-specific checks instead of accepting a
model-built verification report.

### Reviewed experience reuse (opt-in)

`--experience-database` plus `--experience-actor` (JSON with `user_id`, `tenant_id`,
`project_id`, `data_domains`) let a run attach reviewed experience to the recovery prompt. Both are
required together. Only records in the `active` state are read; the prompt receives
the failure class, applicability conditions and the repair, never the question, an expected
answer, the counterexample or the owner. Hints are attached only after the deterministic
repair budget has already allowed another attempt, so experience cannot widen the retry
budget. An unusable store drops the hints and records `experience_unavailable` in the trace
without changing the task outcome.

A retrieved hint is queued first. Its positive `experience_used.n` is committed only after the
next model invocation returns a response with that prompt; a last-step, pre-call time/cost stop,
or invocation error does not claim delivery. This is conservative application-level evidence,
not a provider receipt: an invocation that raises may have reached a remote service, but delivery
is left unconfirmed. Budget drops remain recorded with `n: 0` if queued hints never reach a
confirmed invocation. Historical records used queue-time accounting and remain unchanged; compare
runs made with the same current execution fingerprint rather than reinterpreting old outcomes.

Hint text is admitted whole under a 2000-character default budget, measured on the JSON list
used in the recovery prompt: default ASCII escaping, list brackets and separators all count.
This limits the serialized hint list, not tokens or the surrounding feedback fields. A lesson
whose repair text cannot fit is dropped rather than truncated, because half an
instruction is worse than none; the count is recorded as `dropped` on the case's
`experience_used` entry (with `n: 0` when nothing fit), so a silent size failure cannot look like
"the store had nothing to say". The agent may also hold a smaller per-call cap
(`max_experience`, 2 by default) than the port retrieves (3 by default); hints removed by that
cap are added to `dropped`, so truncation is reported rather than looking like delivery. A cap of
zero disables the port entirely and records nothing. The run summary counts `cases_with_hints`
only where a hint's `n`
is above zero, sums `hints_offered` and `hints_dropped`, and omits zero-count failure codes, so a
case whose lessons were all dropped is not reported as served. The comparison diagnostic reports
`experience_delivery.candidate_hints_dropped`. This bounds prompt size only: it does not rank
lessons by usefulness or guarantee that an admitted hint is relevant. Both the per-case evidence
and the summary are worker self-reports, not independent observation. Budget counters reset
before every lookup. A failed lookup records `experience_unavailable` and contributes no budget
drops; the agent reads drop metadata only after a successful call, so an earlier step's drops
cannot be counted again after a store failure.

Before any case runs, the source store is audited read-only. A file without the experience
schema, or a store whose audit reports findings, stops the run as a usage error before an output
directory is created; an unreadable file surfaces as a `sqlite3.DatabaseError`. After copying the
source, the runner also audits `experience-snapshot.sqlite`, the file workers will actually read.
A failed snapshot audit stops before workers start (exit 2 for missing schema or consistency
findings), retaining the rejected snapshot without a completed report. The report's
`experience_preflight` record count, `consistent: true` and findings count come from this second
audit; `experience_store_hash` identifies the same snapshot. A consistent source update between
audits is allowed and reflected in both the snapshot count and hash. This prevents a stale source
audit from qualifying a changed copy. These checks establish store-internal consistency only:
they do not re-verify source feedback, reviewer identity or trace authenticity. The runner passes
that recorded `experience_store_hash` through `run_isolated` to each worker, which hashes the
snapshot before opening its read-only store. A missing, invalid or mismatched hash makes the
advisory port raise `experience_snapshot_hash_mismatch` when requested; the agent records
`experience_unavailable` and keeps the task's own outcome and budgets. An isolated run that enables
experience must supply the store, the actor scope and the audited snapshot hash together; the
off arm supplies none of them. Every partial combination, including a hash without a store or
actor, is refused before a subprocess starts. With all three present, the hash must be a string
of exactly 64 lowercase hexadecimal characters (without a `sha256:` prefix); invalid formats
raise `ValueError("invalid experience snapshot hash")`. This format check does not read or qualify
the file: the worker still compares its actual bytes, detecting replacements made before that
check, including an internally consistent replacement. It does not make the file
immutable: changes between audit and hashing, between hashing and opening, or during later
lookups, including SQLite sidecar changes, remain outside this check.

The worker builds its hint reader inside the isolated process, so a failure there is a different
kind of fault from a failed task. When the snapshot cannot be opened or the supplied actor scope
does not validate, the worker keeps a hint port that raises on first use instead of dropping it:
`BoundedSQLAgent` then records `experience_unavailable` at the step that asked, and the case keeps
its own failure code, repair budget and tokens. Removing the port would be indistinguishable from
the off arm in the comparison evidence; letting the exception escape would turn every case into
`worker_error` and attribute an infrastructure fault to the tasks. The preflight above normally
catches this before workers start, so this path covers a snapshot that becomes unusable between
preflight and execution.

Capture rejects answer-material markers such as `expected_answer`, `reference SQL`,
`ground_truth label` and `gold labels` without regard to letter case, including inside condition
members. This is a content-shape filter, not proof that a lesson is free of evaluation labels.
Existing records are not automatically rescanned when the capture filter changes; independent
review and dataset separation remain necessary.

Capture binds `owner` to the acting user's `user_id`; a different owner raises
`experience_owner_mismatch` before insertion. State, review and retrieval also reject stored
owner/scope inconsistencies as `experience_integrity_failure`, including legacy rows with valid
body hashes. Such rows need fresh curation and review; readers never silently rewrite ownership.
State, review, retrieval and the consistency audit also bind each decoded `experience_id` to its
SQLite row ID, even when the body's content hash is valid. A mismatch fails reads with
`experience_integrity_failure` and is an `unreadable_record` audit finding, so one row cannot
borrow another identity's receipt or review evidence. No record IDs are repaired automatically.
The review API remains a trusted operator port and does not authenticate reviewer identities.

`ExperienceStore.capture_from_feedback(record, actor, registry=..., dataset_id=...,
feedback_id=...)` accepts only a member of a scoped frozen `training` dataset read through
`FeedbackRegistry.read_dataset`. Evaluation datasets are refused. Candidate insertion and a
separate `experience_feedback_sources` provenance row commit atomically. The row contains only
experience/dataset/feedback/task identifiers, dataset and result hashes, and the training split;
feedback questions and expected outcomes are not copied. Existing capture filters and owner
checks still apply, and feedback review never substitutes for independent experience review.
Writable stores add this provenance table on opening; legacy read-only snapshots remain readable.
This is an opt-in operator API: manual capture and offline extraction do not acquire split
provenance automatically. The provenance row is a capture receipt, not a runtime authorization
check or a cryptographic proof linking the supplied lesson to feedback or execution traces.
`feedback_source(experience_id, actor)` exposes this receipt for operator review after scope and
record validation. It verifies the exact receipt fields, training split, digest and experience ID
within one read transaction. Malformed or mismatched receipts raise `experience_integrity_failure`.
Manual records, missing receipt rows and legacy snapshots without the table return `None`; absence
is not proof of training provenance. The method neither re-reads the source registry nor inserts
missing receipts, and source identifiers never enter runtime lesson briefs.
`verify_feedback_source(experience_id, actor, registry=...)` additionally re-reads the scoped
frozen dataset and checks its training split, exact dataset digest, unique feedback membership,
and task/result binding against the receipt. Missing receipts or mismatched bindings are refused;
registry access and integrity failures propagate. This read-only observation neither activates a
candidate nor records approval. The two databases do not share an atomic snapshot, and verification
of frozen membership does not revalidate current reviewer enrollment or establish trace authenticity.
For a candidate with a stored feedback receipt, `review(..., accepted=True, registry=...)` now
requires that same source verification before inserting a vote. The experience write transaction
holds its immediate lock while checking the receipt and registry, preventing concurrent local
receipt replacement before the vote commits. Missing registry, malformed receipt, source mismatch
or registry failure writes no vote. Rejection remains available without consulting the registry,
so a source outage cannot prevent withdrawal. Manual and legacy records without receipts keep the
manual review path.

Capture now also records an `experience_origins` row (`manual`, `feedback` or `extraction`) in
the same local transaction as the record and its receipt. Readers require the marker to agree
with receipt presence: deleting only the receipt raises `experience_integrity_failure` on state,
retrieval, receipt reads and both accept and reject reviews, instead of quietly restoring the
manual path. Writable opening backfills missing markers from feedback and extraction receipts,
falling back to manual only when neither exists. Existing markers are never overwritten, and
conflicting receipts still fail integrity checks. Read-only legacy snapshots infer the origin
from receipts without creating a table or marker. Removing both the receipt and its marker still looks like a legacy manual record, so this
is tamper evidence for partial deletion, not an anti-deletion guarantee, and it gives no
cross-database atomicity or runtime source recheck.

Every read path validates the receipt a record claims, not just that a receipt row exists. A
feedback-derived record whose receipt is unreadable, fails its digest, or names another
experience raises `experience_integrity_failure` from `state()`, `retrieve()` and
`feedback_source()`, even when the lesson body and review rows are intact. Extraction-derived
records are checked the same way. An unreadable receipt is therefore never presented as an
ordinary candidate awaiting review.

### Read-only experience store audit

```bash
UV_CACHE_DIR=.cache/uv uv run --frozen python evals/audit_stage10_experience.py --store .state/stage10/experience.sqlite --output .artifacts/stage10/experience-audit.json
```

Normal reads validate only the records they touch, so an activated record can carry corruption
until something reads it. This audit sweeps every row in one read transaction and checks the
record body and digest, the origin marker against the receipts, the capture receipt when the
record claims one, and the review rows against the stored state. The report counts records,
distinct scopes, origin and state distributions, and lists one finding per bad record
(`unreadable_record`, `origin_mismatch`, `invalid_capture_receipt`, `invalid_review_evidence`,
`invalid_scope`); a record with several defects reports the first check that failed. After the
body, receipt and review checks, scope values must match `sha256:` followed by 64 lowercase
hexadecimal characters. This includes candidate records and active records hidden from normal
retrieval by a damaged scope. Existing counts still include every stored row and distinct scope
value, including malformed values. A well-formed scope digest does not prove that it belongs to
the record owner or any supplied actor; that relationship needs scoped runtime checks. Only record ids and stable
codes are emitted — never lesson text, owner identity or scope values — and finding ids are
absent for a clean store.

The same transaction also checks association rows whose parent experience is missing. These
produce `orphan_review`, `orphan_origin`, `orphan_feedback_receipt` or
`orphan_extraction_receipt` findings, one per table and missing parent ID; multiple reviewers do
not multiply the finding. Orphans do not increase the main-record or scope counts, but do make
`consistent` false, including when no main records remain. Missing optional provenance tables
in legacy stores are still allowed. The audit never deletes orphan evidence or emits its body.

The audit opens the store read-only, never repairs it, and writes a new report inside the
implementation workspace. Exit code is 0 when consistent and 1 when findings exist. It proves
internal consistency only: it does not re-read the source feedback registry, authenticate the
capture trace, or make deleted receipts recoverable.

An `active` marker alone is insufficient: readers also validate the record's review rows.
Reviewers must be nonblank and different from the owner, verdicts must be integer 0/1, and
activation requires at least one review with every verdict accepting. Missing or inconsistent
review evidence raises `experience_integrity_failure`. State and retrieval checks use one SQLite
read transaction; a concurrent withdrawal becomes visible on the next read. Review writes take
an immediate transaction before checking existing evidence. This validates local consistency,
not reviewer authentication or tamper-proof provenance; coordinated database rewrites remain
outside this guarantee.

Before running cases, SQLite's backup API creates `experience-snapshot.sqlite` in the fresh
output directory, including committed WAL contents. Every worker reads this copy; changes to
the source store during the run do not change its hints. `experience_store_hash` describes the
saved snapshot, not the live source file. An unreadable or invalid SQLite source fails before
any case worker starts. Workers open `ExperienceStore(..., read_only=True)`: SQLite `mode=ro`
connections validate required tables/columns without schema creation, and capture/review are
refused. Missing or incomplete stores are never silently initialized by the evaluation worker.
Workers verify its recorded SHA-256 before opening; they refuse hints on a mismatch. This does
not prevent external modification or bind subsequent SQLite reads to immutable bytes.

Enabling reuse sets `experience_reuse`, `experience_store_hash` and `experience_scope_hash`
in the protocol, so the general pairing gate rejects an off/on pair. The dedicated treatment
diagnostic below preserves those distinct protocols. The scope hash covers the validated actor,
with data domains sorted and deduplicated exactly as retrieval scopes are. Different users,
tenants, projects or domain sets therefore change the protocol even when the store file is
identical. Only the hash is included in the report, not raw actor identifiers. For comparisons
between two enabled runs, use the same store snapshot and actor scope. Historical reports
missing this hash cannot establish scope equality and must not be backfilled with a later
configuration.

```bash
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=.cache/uv uv run --frozen python evals/run_stage6.py --benchmark dabstep --limit 10 --workers 1 --app-type magpie --provider-id magpie --model workbuddy/deepseek-v4.1-flash --transport openai-chat --csv-importer identifier-v2 --request-interval-seconds 3.5 --tool-profile sql-rules --evaluation-protocol stage9-v1 --experience-database .state/stage10/experience.sqlite --experience-actor '{"user_id":"eval","tenant_id":"eval","project_id":"dabstep","data_domains":["payments"]}' --output .artifacts/stage10/dabstep-experience
```

The summary reports `experience_reuse` with the number of cases that received hints, the
number of hints offered and their distribution by failure code. Worker timeouts, worker errors
and service-circuit results carry an empty `experience_used` list so delivery evidence exists for
every planned case, including cases that never reached the model.

### Experience off/on treatment diagnostics

```bash
UV_CACHE_DIR=.cache/uv uv run --frozen python evals/compare_stage10_experience.py --baseline .artifacts/stage10/off/report.json --candidate .artifacts/stage10/on/report.json --output .artifacts/stage10/experience-comparison-fresh.json
```

Both inputs must be completed runs with the full planned denominator, identical ordered case
IDs and database hashes, and matching model, route, runner, sources and non-experience system
fingerprints. Each corresponding case must also retain the same `sub_total` and `difficulty`;
equal aggregate subquestion totals cannot compensate for changed per-case scoring denominators.
The baseline must have reuse disabled; the candidate must have it enabled with
store and actor-scope hashes. Only the three declared experience protocol fields and the
validated experience fingerprint may differ. Each original protocol hash is checked and kept;
input reports are never rewritten. The output must be new and inside this repository.

The diagnostic reports per-case improvements and regressions, failure transitions, tokens,
latency and available cost. Unknown cost stays unknown. Service failures, unattempted cases
and severe incidents stay in the denominator and add attribution blockers. If either arm lacks
complete token telemetry, `blocked_by` includes `incomplete_token_telemetry`; observed token
subtotals, outcome transitions and every planned case remain available. This flags incomplete
resource evidence without imputing missing usage or changing the observed accuracy comparison.
Delivery is checked separately from the declared treatment. Every case row must carry an
`experience_used` list; missing or malformed records are refused rather than treated as "no
hints". Within each case, recorded steps must strictly increase: duplicate and reversed steps
are refused rather than counted again. Steps may skip, and ordering restarts for each case.
This validates the worker's one-entry-per-recovery-step structure; it does not authenticate trace
execution. The off arm must have empty `experience_used` lists: delivered hints raise
`off arm received experience`, and budget-only drops raise `off arm contains experience budget
drops`, since they still indicate experience processing in a declared disabled arm. Budget-only
entries in the on arm remain valid and retain their drop counts. When the on arm delivered
no hint to any case, `blocked_by` includes `experience_treatment_not_delivered`, so a declared
but never-arriving treatment is not scored as evidence of no effect. Each case trace must be a
list of objects with a positive integer `step` and a nonblank string `status`; a missing or
malformed trace raises `experience trace evidence invalid` instead of reading as healthy. An
`experience_unavailable` event in the off arm raises `off arm recorded experience processing`,
because a disabled arm must not consult the advisory port at all. The same event in the on arm
adds `experience_unavailable_confound_attribution`, even when other cases received hints. An
advisory port outage must not be hidden by successful delivery elsewhere; all cases, outcomes and
usage remain reported. A case cannot claim hint delivery or budget drops at a step that also
records `experience_unavailable`: the worker emits neither when that lookup fails. Such a
contradiction raises `experience delivery conflicts with unavailable step`. An outage and
hint processing at separate steps, or at the same step number in different cases, remain valid
and keep the outage blocker. This checks consistency of supplied evidence without authenticating
trace completeness.
The result reports
`experience_delivery` with per-arm case counts, the candidate's total hint count, the count dropped
for exceeding the character budget, and the distribution by failure code. Hints are self-reported trace entries, so this shows that the
treatment was applied, not that a hint caused an outcome.
Report hashes identify supplied evidence; they do not authenticate the inputs. An empty blocker list is not
proof of benefit. Statistical inference, three repeated pairs, unseen-task evaluation,
independent review and production qualification remain separate and are marked `not_run`.
The general pairing gate continues to reject unequal protocols.

### Offline candidate extraction

`evals/extract_stage10_experience.py --trace <trace-array.json> --documents <manual.txt>
--actor '<ActorContext JSON>' --output <fresh-directory>` creates a separate
`candidates.sqlite` and provenance report. The output must be new and inside this repository;
there is no automatic review or merge into a runtime experience store.

Each candidate is captured through `capture_from_extraction`, which re-runs the template over the
supplied bytes and refuses a record the bytes do not reproduce; asserting matching digests is not
enough. The stored `experience_extraction_sources` receipt holds only template version, step
numbers, result hash and the trace, document and extractor digests — never question text, excerpt
text or expected answers. Such records are marked `origin='extraction'`; `extraction_source()`
returns the validated receipt for review, and any read or review of a record whose receipt is
missing, malformed or inconsistent raises `experience_integrity_failure`. Extraction provenance
does not replace independent experience review and does not by itself qualify a record for runtime
retrieval. Deleting both the receipt and its origin marker still looks like an older manual record.

The first template supports only `rule_semantics_excerpt_missing` followed immediately by a
successful `fee_rules` call at the next step. The failure trace must contain the hash of all
call parameters except `semantics_excerpt`; those parameters must remain unchanged. Successful
arguments must also pass the strict `RuleAnalysisRequest` contract, and the result evidence must
identify that same operation. A matching hash cannot substitute for valid typed arguments or
consistent operation evidence. The extractor checks the successful result hash, required rule
checks, and verbatim null excerpt
against the supplied documents. Old traces lacking the parameter binding produce no candidate.
Rule profiles include the extraction module in their system fingerprint because it computes
this binding. Only repository-owned generic prose enters the lesson; trace content is not
copied into its repair or counterexample. The report records input hashes and step provenance.
The CLI reads each input once as bytes, decodes UTF-8 for extraction and hashes those same
bytes for provenance. Replacement or deletion after reading cannot change the reported input
hashes. This is a per-file memory snapshot, not an atomic snapshot across both files; it does
not authenticate their origin or persist a copy of potentially sensitive raw inputs.

This is development evidence from caller-supplied traces, not authenticated production proof.
A successful tool call establishes neither answer correctness nor the correctness of the
chosen semantic interpretation. Independent review remains `not_run`, and retrieval cannot
see the candidate until the existing review gate accepts it. Other failure classes, automatic
batch report ingestion, unseen-task benefit and production qualification remain unimplemented.

## Historical v7 reproduction

The following examples came from the historical v7 development record. Use the matching pinned source to reproduce that run; current defaults and protocol hashes differ. All commands run from the implementation repository and use fresh output paths. The report builder verifies summary digests, paired protocols, and stored prediction scoring without calling a model. Its manifest contains report locations, not credentials or answer labels. Historical results remain [outside the runbook](../../../docs/progress/stage6-iteration-v7.md).

```bash
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/run_stage6.py --benchmark infi --limit 60 --workers 1 --app-type claude --provider-id PROVIDER_ID --model nvidia/nemotron-3-super-120b-a12b --transport openai-chat --thinking off --json-mode --request-interval-seconds 3.5 --csv-importer identifier-v2 --tool-profile sql-repair --output .artifacts/stage6/infi-repair-fresh
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/qualify_stage6_supervisor.py --provider-id PROVIDER_ID --model nvidia/nemotron-3-super-120b-a12b --bound-tools --output .artifacts/stage6/supervisor-bound-fresh/report.json
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/qualify_stage6_repair.py --report .artifacts/stage6/dabstep-nvidia-enhanced-v5/report.json --output .artifacts/stage6/feedback-replay-fresh.json
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/build_stage6_iteration_report.py --manifest evals/stage6_iteration_v7.json --output ../docs/validation/stage6-iteration-v7-results
```

Documents are opt-in and are exercised only when `search_documents` is called. In a paired run,
evidence of use comes from persisted `rule.document.evidence` artifacts under each task's thread
directory — one per search, carrying `status` (`matches` or `gap`), query and citations — not from
the model's narration. Compare those counts between the two arms before attributing any answer
change to the treatment; a treatment arm that made no searches has not tested documents.
