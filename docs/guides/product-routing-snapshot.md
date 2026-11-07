# Product routing snapshots

Stage 15.1A adds an opt-in, deterministic record of one admitted model route. It is a consumer of
the existing `RoutingRequest`, `ModelRoute`, `CapabilityRegistry`, `TaskBudgetLedger` and
`GovernedModelExecutor`. It does not select a route, call a provider, expose an endpoint, or change
the fixed certified-metric graph. See [ADR 0027](../adr/0027-product-routing-snapshot.md).

## What the snapshot contains

`RoutingSnapshot` is a frozen v1 contract with two digests:

- `decision_digest` binds the task ID, decision ID, exact route name/version/content hash and the
  full `RoutingRequest`.
- `snapshot_digest` binds the canonical snapshot payload, including the admitted capability,
  model/configuration identity, timeout, six fixed downgrade conditions, price, reservation,
  observation and settlement status.

A reservation is an authorized upper bound. An observation is accepted only as a complete set of
provider-reported token counts, tool calls, elapsed time and cost derived from operator-pinned
unit prices. A cache hit, failed call, cancelled call or missing provider measurement has
`observation.source=unknown` and `settlement_status=unknown`; it is never recorded as zero-cost
success. An unknown price refuses the decision rather than manufacturing a quote.

## Enable one reviewed route

Snapshots are off by default. An operator must enroll a reviewed `RouteSnapshotSpec` under the
exact `(route.name, route.version)` pair on a `GovernedModelExecutor`:

```python
from generaldataagent.contracts.routing_snapshot import RouteCapability
from generaldataagent.routing.snapshot import RouteSnapshotSpec

route = executor.routes[0]
executor.snapshot_specs[(route.name, route.version)] = RouteSnapshotSpec(
    capability=RouteCapability(
        kind="model",
        name=route.name,
        version=route.version,
        content_hash=route.content_hash,
        purposes=("narrative",),
    ),
    model_name=route.name,
    configuration_hash=route.content_hash,
    price_version="operator-prices-2026-11",
    price_known=True,
    timeout_seconds=15,
    tool_calls=1,
)
```

The spec must match the admitted route pin, the request timeout (`timeout_seconds * 1000` ms),
the purpose/classification and the request budget. A disabled route, changed content hash,
unavailable price version, non-admitted purpose or incomplete provider usage fails closed. The
executor still chooses the cheapest admitted candidate and keeps existing authorization, budget,
deadline and DLP checks.

With no enrolled spec, `execute()` returns `routing_snapshot=None`. With a reviewed spec,
`RoutedNarrator` passes the declaration through `model_usage`; `MetricAnalysisRuntime` then
validates the same-task binding, writes a `routing_snapshot` artifact and adds it to
`DeliveryPackage.evidence`. No factual claim is produced from the snapshot or model prose.

## Run the focused regression

```bash
uv run pytest tests/test_stage15_routing_snapshot.py
```

The tests cover pinned unit prices versus measured cost, digest tampering, changed route/request
pins, unknown observations, cache hits, the opt-in executor consumer and same-task artifact
evidence. A live model route, real price feed and paired performance claim are not part of this
slice.

## Boundaries

This slice does not implement parent/child budget settlement or release, bounded concurrency,
dynamic subagents, an interpreter, a new API/UI, a fallback decision, independent cost
recalculation, or an end-to-end p50/p95 and per-success-task cost study. Research evaluation
routing remains distinct from product readiness. The production constructor gates are unchanged:
`independent_review=not_run`, `production_accepted=false`.
