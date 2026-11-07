# ADR 0027: Pin product route decisions in a digest-bound routing snapshot

Status: accepted for the opt-in deterministic routing consumer; no route chooser, API, UI, live
provider qualification or production admission change.

## Context

Stage 15.1A requires a product task to record the model/version, admitted capability, timeout,
downgrade conditions and cost source used for one execution. Stage 10 already has
`RoutingRequest`, `ModelRoute`, `GovernedModelExecutor`, `CapabilityRegistry` and
`TaskBudgetLedger`, but a result alone does not distinguish an operator-pinned price from a
provider measurement, a reserved upper bound from actual usage, or a cache hit from a zero-cost
model call. Research evaluation routes must not be presented as product measurements.

The snapshot cannot trust prose from a model, invent a missing price, or treat missing telemetry as
zero. It must fail closed when the route pin, admission, timeout, unit price or provider usage is
incomplete or inconsistent. It also must not change which route the existing executor selects.

## Decision

Add the versioned `RoutingSnapshot` contract, `RouteSnapshotSpec` and
`RouteSnapshotTracker`. A snapshot is bound to one task, decision and admitted route. Its
`decision_digest` covers the task/decision/route pin and the complete `RoutingRequest`; its
`snapshot_digest` covers the canonical payload including capability, model/config identity,
timeout, six fixed downgrade conditions, price, reservation, observation and settlement status.

`RouteSnapshotSpec` is reviewed operator configuration. It validates its exact
model/configuration pin, price-version state, timeout, tool-call bound, name and hash shapes.
`RouteSnapshotTracker.create()` refuses a changed capability/route pin, a non-admitted
purpose/classification, a timeout disagreement, a disabled or over-budget route, an implausible
latency estimate, an invalid decision ID and a missing price version. `snapshot()` accepts
provider-reported usage only as one complete set and derives observed cost from the pinned unit
prices, refusing a measurement above the reserved bound. A cache hit, a failed or cancelled call
and missing provider usage stay `observation.source=unknown` with
`settlement_status=unknown`, never zero-cost success.

`GovernedModelExecutor` gains an empty `snapshot_specs` map. A reviewed `RouteSnapshotSpec`
enrolled for an exact `(name, version)` pair enables snapshots for that route; absence keeps the
existing result shape with `routing_snapshot=None`. The executor still selects the cheapest
admitted route, reserves budget, checks current authorization/admission, enforces the timeout
and validates provider token bounds. The tracker is a consumer of those facts, not a router.

When `RoutedNarrator` returns an admitted snapshot, `MetricAnalysisRuntime` validates it,
requires its `task_id` to equal the current task, writes a same-task `routing_snapshot`
artifact and includes that artifact in the delivery evidence. No claim is added from model text.
A foreign-task or malformed snapshot is refused before delivery.

Three execution paths exist in the versioned contract: `certified_metric`,
`fixed_science_tool` and `admitted_model`. Only the admitted model path is constructed by this
slice. Fixed science and certified metric paths require their own capability kind and cannot
declare model identity; a simple certified metric remains on its existing fixed graph.

## Consequences

A reviewer can distinguish a pinned price version from a live quote, an authorized reservation
from a provider-reported observation, and a cached result from a measured call. Changing a
decision field without recomputing both digests is rejected. A price or usage gap is explicit
and cannot be silently converted into a settled cost.

This slice does not add concurrency, parent/child budget settlement, dynamic subagents, an
interpreter, a live pricing source, a new route-selection policy, a new model endpoint, an API
or UI, or an independent cost recalculation. It does not claim an end-to-end cost or latency
improvement, enterprise identity, external review or production readiness.
`independent_review=not_run`, `production_accepted=false`.
