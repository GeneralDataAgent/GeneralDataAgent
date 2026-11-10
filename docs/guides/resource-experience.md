# Resource and experience measurement

Stage 15.1D summarizes declared end-to-end task samples. It separates queue time, latency,
model calls, estimated usage and measured usage; it does not generate load or call a model.

```python
from generaldataagent.contracts.child_budget import BudgetUsageAmount
from generaldataagent.contracts.resource_experience import (
    EstimatedResource,
    ResourceExperienceSample,
)
from generaldataagent.evaluation.resource_experience import (
    compare_resource_experience,
    summarize_resource_experience,
)

sample = ResourceExperienceSample(
    task_id="metric-population",
    passed=True,
    queue_ms=150,
    latency_ms=1_200,
    model_calls=2,
    estimated=EstimatedResource(
        tokens=500,
        tool_calls=4,
        scan_bytes=10_000,
        cost_microunits=500,
        runtime_milliseconds=1_800,
    ),
    measured=BudgetUsageAmount(
        source="provider_reported",
        tokens=420,
        tool_calls=3,
        scan_bytes=8_000,
        cost_microunits=410,
        runtime_milliseconds=1_050,
        complete=True,
    ),
)
report = summarize_resource_experience([sample])
comparison = compare_resource_experience(baseline_samples, candidate_samples)
```

`report` contains p50/p95 latency and queue metrics, total model calls, measured and estimated
usage totals, measured-to-estimated ratios, and cost per successful task. If any sample lacks
complete measured usage, usage totals and cost per success are `None`, and
`unknown_usage_tasks` says how many are unknown. They are never replaced with zero.

`comparison` requires the same unique task set in both arms. It prefers the candidate only when
success rate is not lower, at least one measured endpoint improves by at least 5%, and no p95
latency or known cost-per-success regression exceeds 2%. The thresholds are arguments, and the
result always reports `causal_attribution=False`.

This is a deterministic measurement contract, not a load test, statistical test, billing
source, live-model benchmark, or production qualification. Research evaluation routing does not
establish production usability.
