"""Build immutable routing snapshots for one admitted model call.

Stage 15.1A is a deterministic consumer of existing routing facts. The tracker
does not select a route, call a provider, write artifacts, decide a fallback,
or change production admission. It refuses a request when its route/config
pin, admitted purpose/classification, transport timeout, route price or usage
observation is incomplete. A cache hit, a failed or cancelled call, and a
missing provider measurement all stay ``unknown`` — never a zero-cost success.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from generaldataagent.business.optimization import ModelRoute, ProviderResult, RoutingRequest
from generaldataagent.contracts.routing_snapshot import (
    DOWNGRADE_CONDITIONS,
    RouteCapability,
    RouteObservation,
    RoutePrice,
    RouteReservation,
    RoutingSnapshot,
    routing_snapshot_payload,
)
from generaldataagent.memory.display_preferences import canonical_hash

__all__ = [
    "RouteSnapshotRefused",
    "RouteSnapshotSpec",
    "RouteSnapshotTracker",
    "settled_observation",
]


class RouteSnapshotRefused(ValueError):
    """A route decision cannot be represented without inventing a fact."""


@dataclass(frozen=True, slots=True)
class RouteSnapshotSpec:
    """Reviewed, operator-owned configuration for one admitted model route."""

    capability: RouteCapability
    model_name: str
    configuration_hash: str
    price_version: str
    price_known: bool
    timeout_seconds: int
    tool_calls: int

    def __post_init__(self):
        if not isinstance(self.capability, RouteCapability):
            raise RouteSnapshotRefused("routing_snapshot_capability_pin_invalid")
        if (
            self.capability.kind != "model"
            or self.capability.name != self.model_name
            or self.configuration_hash != self.capability.content_hash
        ):
            raise RouteSnapshotRefused("routing_snapshot_capability_pin_invalid")
        if type(self.price_known) is not bool:
            raise RouteSnapshotRefused("routing_snapshot_price_state_invalid")
        if self.price_known and self.price_version == "unknown":
            raise RouteSnapshotRefused("routing_snapshot_price_version_missing")
        if not self.price_known and self.price_version != "unknown":
            raise RouteSnapshotRefused("routing_snapshot_price_version_unexpected")
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 60:
            raise RouteSnapshotRefused("routing_snapshot_timeout_invalid")
        if type(self.tool_calls) is not int or not 1 <= self.tool_calls <= 1000:
            raise RouteSnapshotRefused("routing_snapshot_tool_calls_invalid")
        if type(self.model_name) is not str or not re.fullmatch(
            r"^[a-z][a-z0-9_.-]{0,127}$", self.model_name
        ):
            raise RouteSnapshotRefused("routing_snapshot_model_name_invalid")
        if type(self.configuration_hash) is not str or not re.fullmatch(
            r"^sha256:[0-9a-f]{64}$", self.configuration_hash
        ):
            raise RouteSnapshotRefused("routing_snapshot_configuration_hash_invalid")
        if type(self.price_version) is not str or not 1 <= len(self.price_version) <= 128:
            raise RouteSnapshotRefused("routing_snapshot_price_version_invalid")


@dataclass(frozen=True, slots=True)
class RouteSnapshotTracker:
    """One admitted route decision, with settlement as a separate explicit step."""

    task_id: UUID
    decision_id: str
    spec: RouteSnapshotSpec
    route: ModelRoute
    request: RoutingRequest
    decision_digest: str

    @classmethod
    def create(
        cls,
        *,
        task_id: UUID,
        decision_id: str,
        spec: RouteSnapshotSpec,
        route: ModelRoute,
        request: RoutingRequest,
    ) -> RouteSnapshotTracker:
        if not isinstance(spec, RouteSnapshotSpec):
            raise RouteSnapshotRefused("routing_snapshot_input_invalid")
        if type(decision_id) is not str or not re.fullmatch(
            r"^[a-z][a-z0-9_.-]{0,127}$", decision_id
        ):
            raise RouteSnapshotRefused("routing_snapshot_decision_id_invalid")
        if not isinstance(route, ModelRoute) or not isinstance(request, RoutingRequest):
            raise RouteSnapshotRefused("routing_snapshot_input_invalid")
        if type(task_id) is not UUID:
            raise RouteSnapshotRefused("routing_snapshot_task_invalid")
        if (route.name, route.version, route.content_hash) != (
            spec.capability.name,
            spec.capability.version,
            spec.capability.content_hash,
        ):
            raise RouteSnapshotRefused("routing_snapshot_route_pin_mismatch")
        if request.purpose not in spec.capability.purposes:
            raise RouteSnapshotRefused("routing_snapshot_purpose_not_admitted")
        if request.classification not in route.classifications:
            raise RouteSnapshotRefused("routing_snapshot_classification_not_admitted")
        if request.max_latency_ms != spec.timeout_seconds * 1000:
            raise RouteSnapshotRefused("routing_snapshot_request_timeout_mismatch")
        if route.estimated_latency_ms > request.max_latency_ms:
            raise RouteSnapshotRefused("routing_snapshot_latency_bound_exceeded")
        if route.enabled is not True:
            raise RouteSnapshotRefused("routing_snapshot_route_disabled")
        if route.cost_bound(request) > request.max_cost_microunits:
            raise RouteSnapshotRefused("routing_snapshot_cost_bound_exceeded")
        if not spec.price_known:
            raise RouteSnapshotRefused("routing_snapshot_price_missing")
        digest = canonical_hash(
            {
                "task_id": str(task_id),
                "decision_id": decision_id,
                "route": [route.name, route.version, route.content_hash],
                "request": request.model_dump(mode="json"),
            }
        )
        return cls(
            task_id=task_id,
            decision_id=decision_id,
            spec=spec,
            route=route,
            request=request,
            decision_digest=digest,
        )

    def snapshot(
        self,
        *,
        reserved_cost_microunits: int,
        result: ProviderResult | None = None,
        elapsed_ms: int | None = None,
        cached: bool = False,
    ) -> RoutingSnapshot:
        """Render one decision; observations are accepted only as a complete set."""
        if type(cached) is not bool:
            raise RouteSnapshotRefused("routing_snapshot_settlement_state_invalid")
        if type(reserved_cost_microunits) is not int or reserved_cost_microunits < 0:
            raise RouteSnapshotRefused("routing_snapshot_reservation_invalid")
        if cached or result is None:
            if result is not None or elapsed_ms is not None or reserved_cost_microunits != 0:
                raise RouteSnapshotRefused("routing_snapshot_settlement_unexpected")
            observation = RouteObservation(source="unknown", complete=False)
        else:
            observation = settled_observation(
                self.route,
                result,
                elapsed_ms=elapsed_ms,
                cost_microunits=reserved_cost_microunits,
            )
        price = RoutePrice(
            source="pinned_operator_table",
            price_version=self.spec.price_version,
            input_cost_per_token=self.route.input_cost_per_token,
            output_cost_per_token=self.route.output_cost_per_token,
        )
        # There was no provider call on a cache hit; do not present a reserved
        # model cost as if that call had been measured.
        reservation_cost = (
            self.route.cost_bound(self.request) if observation.source == "provider_reported" else 0
        )
        reservation = RouteReservation(
            input_token_bound=self.request.input_token_bound,
            output_token_bound=self.request.output_token_bound,
            tool_calls=self.spec.tool_calls,
            cost_microunits=reservation_cost,
            latency_bound_ms=self.request.max_latency_ms,
        )
        settlement_status = "settled" if observation.source == "provider_reported" else "unknown"
        payload = routing_snapshot_payload(
            task_id=self.task_id,
            decision_id=self.decision_id,
            decision_digest=self.decision_digest,
            path="admitted_model",
            capability=self.spec.capability,
            model_name=self.spec.model_name,
            configuration_hash=self.spec.configuration_hash,
            timeout_seconds=self.spec.timeout_seconds,
            latency_bound_ms=self.request.max_latency_ms,
            max_output_tokens=self.request.output_token_bound,
            max_cost_microunits=self.request.max_cost_microunits,
            downgrade_conditions=DOWNGRADE_CONDITIONS,
            price=price,
            reservation=reservation,
            observation=observation,
            settlement_status=settlement_status,
        )
        return RoutingSnapshot(
            task_id=self.task_id,
            decision_id=self.decision_id,
            decision_digest=self.decision_digest,
            path="admitted_model",
            capability=self.spec.capability,
            model_name=self.spec.model_name,
            configuration_hash=self.spec.configuration_hash,
            timeout_seconds=self.spec.timeout_seconds,
            latency_bound_ms=self.request.max_latency_ms,
            max_output_tokens=self.request.output_token_bound,
            max_cost_microunits=self.request.max_cost_microunits,
            downgrade_conditions=DOWNGRADE_CONDITIONS,
            price=price,
            reservation=reservation,
            observation=observation,
            settlement_status=settlement_status,
            snapshot_digest=canonical_hash(payload),
        )


def settled_observation(
    route: ModelRoute,
    result: ProviderResult,
    *,
    elapsed_ms: int | None,
    cost_microunits: int,
) -> RouteObservation:
    """Derive complete measured usage from provider-reported token counts."""
    if (
        not isinstance(route, ModelRoute)
        or not isinstance(result, ProviderResult)
        or type(elapsed_ms) is not int
        or elapsed_ms < 0
        or type(cost_microunits) is not int
        or cost_microunits < 0
    ):
        raise RouteSnapshotRefused("routing_snapshot_usage_incomplete")
    measured_cost = (
        result.input_tokens * route.input_cost_per_token
        + result.output_tokens * route.output_cost_per_token
    )
    if measured_cost > cost_microunits:
        raise RouteSnapshotRefused("routing_snapshot_observed_cost_exceeded")
    return RouteObservation(
        source="provider_reported",
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        tool_calls=1,
        elapsed_ms=elapsed_ms,
        cost_microunits=measured_cost,
        complete=True,
    )
