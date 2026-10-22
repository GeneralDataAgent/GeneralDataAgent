# Benchmark evaluation protocol design

This reference separates evaluator invariants from the system being tested. Dataset choice, pinned source revisions, planned milestones, and historical results remain in the [Stage 6 plan](../../../docs/planning/stage6-benchmark-plan.md) and [validation records](../../../docs/validation/README.md). Execution commands belong in the [runbook](../operations/benchmark-evaluation-runbook.md).

## Provenance and environment qualification

Check every input hash. Download/extraction is separate from execution. Qualify BIRD reference queries against the actual pinned databases to identify version, timeout, and result-size constraints; environment failures are not automatically model reasoning errors.

## Frozen cohort

The original development cohort uses seed `20261020`, stratified by difficulty and database/file, deterministic within-bucket shuffling and round-robin selection. Sampling never reads answers. Persist IDs and cohort hashes; include all ten DABstep dev tasks.

## Answer isolation

Workers receive only the public Task, authorized schema, one bounded sample row, and necessary business documents. Never send golden labels, reference SQL, scorer objects, or tools for other task files. Pass a task DTO rather than a Case with answers. This is input/tool isolation, not a claim of production filesystem sandboxing.

## Execution and resource policy

Fingerprint action limits, token policy, request/process/query deadlines, and preview limits for every run. Keep the effective resource policy identical across paired arms. Historical configurations are retained in the [Stage 6 plan](../../../docs/planning/stage6-benchmark-plan.md).

Cumulative and per-call output token limits are unset by default. Both arms must share the effective policy; optional explicit finite limits are fingerprinted. Record observed input, output, and total usage. Provider context limits still apply. Existing action/time, SQL, authorization, cost, and side-effect controls remain separately governed. No automatic SDK retries, arbitrary code, shell, database writes, or expanded source privileges are granted by removing token caps.

## Scoring and evidence

- InfiAgent: exact strings or finite numbers with absolute error `<1e-6`, checked against upstream scorer behavior.
- DABstep: disclosed local numeric/string/list comparison.
- BIRD: actual query results, retaining duplicates, NULLs, column order, and numeric tolerance; local comparison ignores row ordering.

These are local protocols, not official EX/VES or leaderboard scores. Non-SQL answers cite successful computations; valid step references alone do not prove semantic entailment.

Record SQL, result hashes/counts, timing, failure categories, final outputs, before/after database hashes, route/prompt/code/protocol fingerprints, and tokens. Include process startup in latency. Missing telemetry is unknown/partial; an unobserved timed-out call is not zero-cost. Unavailable trusted cost stays null.

## Statistical summaries and gates

Report task pass@1, subanswer accuracy, difficulty slices, failure types, P50/P95, and Wilson 95% intervals. Intervals describe the fixed development sample, not an independently sampled population, contamination-free evaluation, or hidden tests.

Reject missing/duplicate tasks, incompatible protocols, altered hashes/summaries, and nonfinite telemetry. Fresh-baseline regressions and original historical-correct regressions are tracked separately. Severe incidents cannot be offset by accuracy. Missing cost, repeat stability, independent review, product-Supervisor qualification, and shadow/canary evidence remain explicit open gates.

Version phase-specific targets in the [external plan](../../../docs/planning/stage6-benchmark-plan.md). Evaluate each track separately; project targets do not establish benchmark-author requirements or production guarantees.
