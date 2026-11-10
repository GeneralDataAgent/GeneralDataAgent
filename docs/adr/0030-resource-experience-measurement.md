# ADR 0030: Measure resources and experience on declared samples

Status: accepted as a deterministic evaluation library; no load generator, live-model benchmark,
billing source or production acceptance.

## Context

Stage 15.1D must compare end-to-end p50/p95, queueing, model/tool usage, and cost per successful
task while keeping actual and estimated values separate. A faster research route does not by
itself prove production usability, and missing usage must not be read as zero.

## Decision

One `ResourceExperienceSample` records a task outcome, queue time, end-to-end latency, model
calls, an optional pre-run estimate and optional measured usage. Queue time must not exceed
latency. Estimated fields are independently optional, so a missing estimate is unknown rather
than zero.

`summarize_resource_experience` computes nearest-rank p50/p95 latency and queue metrics, total
model calls, measured and estimated usage totals, and measured-to-estimated ratios. Usage totals
and cost per successful task are reported only when every sample has complete measured usage;
otherwise they remain `None` and the unknown task count is exposed. The cost denominator is the
number of successful tasks, but incomplete failure costs still suppress the metric rather than
understate total cost.

`compare_resource_experience` requires the same unique task set in both arms. It prefers the
candidate only when success rate is not lower, at least one measured endpoint improves by the
declared relative threshold, and neither p95 latency nor known cost per success regresses beyond
the declared tolerance. The result reports the basis and explicitly sets
`causal_attribution=False`; it is an observation on the declared sample, not a causal or
production claim.

The library does not generate load, call a model, read provider invoices, resample, test
statistical significance, or qualify a route. Those remain separate work.

## Consequences

Baseline and candidate routes can be compared on identical declared tasks without treating
research evaluation routing as production usability. `independent_review=not_run` and
`production_accepted=false`.
