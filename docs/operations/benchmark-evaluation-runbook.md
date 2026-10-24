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
`incomplete_sequence_reason_unknown`. Every non-completed row keeps
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
hash are written to the run manifest, so a misroute is auditable after the fact. A question
matching no marker takes `short`, whose smallest budget makes a misroute visible as a budget
failure rather than silent extra spend.

A repair refusal does not always mean its allowance was exhausted. For nonretryable causes,
including policy denial, the case retains the original failure code and the trace records
`repair_not_applicable_<cause>`; no experience hints are requested. The repair report's
`exhausted` flag (and summary `exhausted_n`) counts refusals caused by the total repair ceiling
or identical-failure ceiling. Other refusals remain in `refused` without setting that flag.
Historical reports retain their original diagnostic fields and must not be silently rewritten.

The system fingerprint includes Stage 7/8/9 sources according to the profiles selected for
this cohort, including when routing replaces the fixed profile. A mixed cohort records the
union of those dependencies. Repair-budget and retry-classification source hashes are also
recorded for fixed-profile runs. These identify the system under evaluation; they do not alter
the scoring protocol or establish model performance. Older manifests with missing dependency
hashes must retain that limitation rather than receive hashes from a later checkout.

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

Before checking selection or fees, the workflow verifier rebuilds its full context from
the bound request and authoritative sources. The saved context must match exactly, including
field types, scope, formula, counts and monthly aggregates. A mismatch yields
`rule_context_mismatch` and no delivery; extra fields are also refused. This is a fresh
source re-derivation through the same application code, not an independent semantic oracle.
Existing completed checkpoints are not retroactively reverified; use a fresh run or an
explicitly changed workflow version when qualifying this verifier change.

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
`project_id`, `data_domains`) let a run attach reviewed experience to the recovery prompt.
Both are required together. Only records in the `active` state are read; the prompt receives
the failure class, applicability conditions and the repair, never the question, an expected
answer, the counterexample or the owner. Hints are attached only after the deterministic
repair budget has already allowed another attempt, so experience cannot widen the retry
budget. An unusable store drops the hints and records `experience_unavailable` in the trace
without changing the task outcome.

Before running cases, SQLite's backup API creates `experience-snapshot.sqlite` in the fresh
output directory, including committed WAL contents. Every worker reads this copy; changes to
the source store during the run do not change its hints. `experience_store_hash` describes the
saved snapshot, not the live source file. An unreadable or invalid SQLite source fails before
any case worker starts. Workers open `ExperienceStore(..., read_only=True)`: SQLite `mode=ro`
connections validate required tables/columns without schema creation, and capture/review are
refused. Missing or incomplete stores are never silently initialized by the evaluation worker.
This does not protect the saved copy from external modification.

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
number of hints offered and their distribution by failure code.

### Experience off/on treatment diagnostics

```bash
UV_CACHE_DIR=.cache/uv uv run --frozen python evals/compare_stage10_experience.py --baseline .artifacts/stage10/off/report.json --candidate .artifacts/stage10/on/report.json --output .artifacts/stage10/experience-comparison-fresh.json
```

Both inputs must be completed runs with the full planned denominator, identical ordered case
IDs and database hashes, and matching model, route, runner, sources and non-experience system
fingerprints. The baseline must have reuse disabled; the candidate must have it enabled with
store and actor-scope hashes. Only the three declared experience protocol fields and the
validated experience fingerprint may differ. Each original protocol hash is checked and kept;
input reports are never rewritten. The output must be new and inside this repository.

The diagnostic reports per-case improvements and regressions, failure transitions, tokens,
latency and available cost. Unknown cost stays unknown. Service failures, unattempted cases
and severe incidents stay in the denominator and add attribution blockers. Report hashes
identify supplied evidence; they do not authenticate the inputs. An empty blocker list is not
proof of benefit. Statistical inference, three repeated pairs, unseen-task evaluation,
independent review and production qualification remain separate and are marked `not_run`.
The general pairing gate continues to reject unequal protocols.

### Offline candidate extraction

`evals/extract_stage10_experience.py --trace <trace-array.json> --documents <manual.txt>
--actor '<ActorContext JSON>' --output <fresh-directory>` creates a separate
`candidates.sqlite` and provenance report. The output must be new and inside this repository;
there is no automatic review or merge into a runtime experience store.

The first template supports only `rule_semantics_excerpt_missing` followed immediately by a
successful `fee_rules` call at the next step. The failure trace must contain the hash of all
call parameters except `semantics_excerpt`; those parameters must remain unchanged. The
extractor checks the successful result hash, required rule checks, and verbatim null excerpt
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
