"""Versioned, digest-bound description of one product execution route.

Stage 15.1A records *what was selected*, not what a model claims it did. The
snapshot declares the execution path, admitted capability/version, model or
fixed-tool identity, timeout, fail-closed downgrade conditions and the source
of unit prices. A reservation is an approved upper bound; an observation is
provider-reported usage. Missing usage is ``unknown``, never zero, and a
missing price cannot be settled as a measured cost.
"""

from __future__ import annotations

import re
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from generaldataagent.contracts.models import ContractModel
from generaldataagent.memory.display_preferences import canonical_hash

ROUTING_SNAPSHOT_VERSION = "1.0"
_DIGEST = r"^sha256:[0-9a-f]{64}$"
_NAME = r"^[a-z][a-z0-9_.-]{0,127}$"
_VERSION = r"^v[1-9][0-9]*$"

ExecutionPath = Literal["certified_metric", "fixed_science_tool", "admitted_model"]
PriceSource = Literal["pinned_operator_table", "unknown"]
ObservationSource = Literal["provider_reported", "unknown"]
SettlementStatus = Literal["settled", "unknown"]

DOWNGRADE_CONDITIONS: tuple[str, ...] = tuple(
    sorted(
        (
            "authorization_revoked",
            "capability_expired",
            "latency_bound_exceeded",
            "price_unavailable",
            "provider_usage_unavailable",
            "route_disabled",
        )
    )
)


class RouteCapability(ContractModel):
    """The admitted capability pin authorizing one route version."""

    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["model", "tool", "fixed_tool"]
    name: str = Field(pattern=_NAME)
    version: str = Field(pattern=_VERSION)
    content_hash: str = Field(pattern=_DIGEST)
    purposes: tuple[str, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def ordered_purposes(self):
        if any(not re.fullmatch(r"^[a-z][a-z0-9_-]{0,63}$", value) for value in self.purposes):
            raise ValueError("routing_snapshot_invalid_purpose")
        if self.purposes != tuple(sorted(set(self.purposes))):
            raise ValueError("routing_snapshot_purposes_unordered")
        return self


class RoutePrice(ContractModel):
    """Operator-pinned unit prices, or an explicit unknown price state."""

    schema_version: Literal["1.0"] = "1.0"
    source: PriceSource
    price_version: str = Field(min_length=1, max_length=128)
    input_cost_per_token: int | None = Field(default=None, ge=0)
    output_cost_per_token: int | None = Field(default=None, ge=0)
    currency: Literal["microunits"] = "microunits"

    @model_validator(mode="after")
    def source_matches_quote(self):
        pinned = self.source == "pinned_operator_table"
        prices = (self.input_cost_per_token, self.output_cost_per_token)
        if pinned and (
            any(type(value) is not int or value < 0 for value in prices)
            or self.price_version == "unknown"
        ):
            raise ValueError("routing_snapshot_pinned_price_incomplete")
        if not pinned and (
            any(value is not None for value in prices) or self.price_version != "unknown"
        ):
            raise ValueError("routing_snapshot_unknown_price_declared")
        return self


class RouteReservation(ContractModel):
    """Authorized upper bounds, not measurements of what was consumed."""

    schema_version: Literal["1.0"] = "1.0"
    input_token_bound: int = Field(ge=1, le=100_000)
    output_token_bound: int = Field(ge=1, le=10_000)
    tool_calls: int = Field(ge=1, le=1_000)
    cost_microunits: int = Field(ge=0)
    latency_bound_ms: int = Field(ge=1, le=60_000)


class RouteObservation(ContractModel):
    """Provider-reported usage, or an explicit unknown observation."""

    schema_version: Literal["1.0"] = "1.0"
    source: ObservationSource
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    tool_calls: int | None = Field(default=None, ge=0)
    elapsed_ms: int | None = Field(default=None, ge=0)
    cost_microunits: int | None = Field(default=None, ge=0)
    complete: bool = False

    @model_validator(mode="after")
    def source_matches_observation(self):
        values = (
            self.input_tokens,
            self.output_tokens,
            self.tool_calls,
            self.elapsed_ms,
            self.cost_microunits,
        )
        if self.source == "provider_reported":
            if any(type(value) is not int or value < 0 for value in values) or not self.complete:
                raise ValueError("routing_snapshot_provider_usage_incomplete")
        elif any(value is not None for value in values) or self.complete:
            raise ValueError("routing_snapshot_unknown_usage_declared")
        return self


class RoutingSnapshot(ContractModel):
    """One route decision with pinned limits, provenance and explicit unknowns."""

    schema_version: Literal["1.0"] = ROUTING_SNAPSHOT_VERSION
    task_id: UUID
    decision_id: str = Field(pattern=_NAME)
    decision_digest: str = Field(pattern=_DIGEST)
    path: ExecutionPath
    capability: RouteCapability
    model_name: str | None = Field(default=None, pattern=_NAME)
    configuration_hash: str | None = Field(default=None, pattern=_DIGEST)
    timeout_seconds: int = Field(ge=1, le=60)
    latency_bound_ms: int = Field(ge=1, le=60_000)
    max_output_tokens: int | None = Field(default=None, ge=1, le=10_000)
    max_cost_microunits: int = Field(ge=0)
    downgrade_conditions: tuple[str, ...]
    price: RoutePrice
    reservation: RouteReservation
    observation: RouteObservation
    settlement_status: SettlementStatus
    snapshot_digest: str = Field(pattern=_DIGEST)

    @model_validator(mode="after")
    def consistent_snapshot(self):
        if self.path == "admitted_model":
            if (
                self.model_name is None
                or self.configuration_hash is None
                or self.max_output_tokens is None
                or self.capability.kind != "model"
            ):
                raise ValueError("routing_snapshot_model_identity_incomplete")
            if (
                self.model_name != self.capability.name
                or self.configuration_hash != self.capability.content_hash
                or self.reservation.output_token_bound != self.max_output_tokens
            ):
                raise ValueError("routing_snapshot_model_pin_mismatch")
        elif self.model_name is not None or self.configuration_hash is not None:
            raise ValueError("routing_snapshot_unexpected_model_identity")
        elif self.max_output_tokens is not None:
            raise ValueError("routing_snapshot_unexpected_output_bound")
        elif self.path == "certified_metric" and self.capability.kind != "fixed_tool":
            raise ValueError("routing_snapshot_certified_metric_capability_invalid")
        elif self.path == "fixed_science_tool" and self.capability.kind != "tool":
            raise ValueError("routing_snapshot_science_tool_capability_invalid")
        if self.downgrade_conditions != DOWNGRADE_CONDITIONS:
            raise ValueError("routing_snapshot_downgrade_conditions_invalid")
        if self.timeout_seconds * 1000 != self.latency_bound_ms:
            raise ValueError("routing_snapshot_timeout_mismatch")
        if self.reservation.latency_bound_ms != self.latency_bound_ms:
            raise ValueError("routing_snapshot_reservation_latency_mismatch")
        if self.reservation.cost_microunits > self.max_cost_microunits:
            raise ValueError("routing_snapshot_reservation_cost_exceeded")
        if self.price.source == "unknown":
            if self.reservation.cost_microunits != 0:
                raise ValueError("routing_snapshot_unpriced_reservation")
            if self.settlement_status == "settled":
                raise ValueError("routing_snapshot_unpriced_settlement")
        else:
            in_price = self.price.input_cost_per_token
            out_price = self.price.output_cost_per_token
            if type(in_price) is not int or type(out_price) is not int:
                raise ValueError("routing_snapshot_pinned_price_incomplete")
            expected_reservation = (
                in_price * self.reservation.input_token_bound
                + out_price * self.reservation.output_token_bound
            )
            # An unknown observation may record no provider reservation; only a
            # settled call claims the full priced upper bound.
            if self.reservation.cost_microunits not in (0, expected_reservation) or (
                self.settlement_status == "settled"
                and self.reservation.cost_microunits != expected_reservation
            ):
                raise ValueError("routing_snapshot_reservation_price_mismatch")
        expected = "settled" if self.observation.source == "provider_reported" else "unknown"
        if self.settlement_status != expected:
            raise ValueError("routing_snapshot_settlement_mismatch")
        if self.observation.source == "provider_reported":
            if self.observation.input_tokens > self.reservation.input_token_bound:
                raise ValueError("routing_snapshot_observed_input_exceeded")
            if self.observation.output_tokens > self.reservation.output_token_bound:
                raise ValueError("routing_snapshot_observed_output_exceeded")
            if self.observation.tool_calls > self.reservation.tool_calls:
                raise ValueError("routing_snapshot_observed_calls_exceeded")
            if self.observation.elapsed_ms > self.reservation.latency_bound_ms:
                raise ValueError("routing_snapshot_observed_latency_exceeded")
            if self.observation.cost_microunits > self.reservation.cost_microunits:
                raise ValueError("routing_snapshot_observed_cost_exceeded")
            in_price = self.price.input_cost_per_token
            out_price = self.price.output_cost_per_token
            if type(in_price) is not int or type(out_price) is not int:
                raise ValueError("routing_snapshot_unpriced_settlement")
            expected_cost = (
                self.observation.input_tokens * in_price
                + self.observation.output_tokens * out_price
            )
            if self.observation.cost_microunits != expected_cost:
                raise ValueError("routing_snapshot_observed_cost_mismatch")
        payload = routing_snapshot_payload(
            task_id=self.task_id,
            decision_id=self.decision_id,
            decision_digest=self.decision_digest,
            path=self.path,
            capability=self.capability,
            model_name=self.model_name,
            configuration_hash=self.configuration_hash,
            timeout_seconds=self.timeout_seconds,
            latency_bound_ms=self.latency_bound_ms,
            max_output_tokens=self.max_output_tokens,
            max_cost_microunits=self.max_cost_microunits,
            downgrade_conditions=self.downgrade_conditions,
            price=self.price,
            reservation=self.reservation,
            observation=self.observation,
            settlement_status=self.settlement_status,
        )
        if self.snapshot_digest != canonical_hash(payload):
            raise ValueError("routing_snapshot_digest_mismatch")
        return self


def _observation_payload(observation: RouteObservation) -> dict:
    return {
        "source": observation.source,
        "input_tokens": observation.input_tokens,
        "output_tokens": observation.output_tokens,
        "tool_calls": observation.tool_calls,
        "elapsed_ms": observation.elapsed_ms,
        "cost_microunits": observation.cost_microunits,
        "complete": observation.complete,
    }


def routing_snapshot_payload(
    *,
    task_id: UUID,
    decision_id: str,
    decision_digest: str,
    path: ExecutionPath,
    capability: RouteCapability,
    model_name: str | None,
    configuration_hash: str | None,
    timeout_seconds: int,
    latency_bound_ms: int,
    max_output_tokens: int | None,
    max_cost_microunits: int,
    downgrade_conditions: tuple[str, ...],
    price: RoutePrice,
    reservation: RouteReservation,
    observation: RouteObservation,
    settlement_status: SettlementStatus,
) -> dict:
    """Canonical bytes bound by ``RoutingSnapshot.snapshot_digest``."""

    return {
        "schema_version": ROUTING_SNAPSHOT_VERSION,
        "task_id": str(task_id),
        "decision_id": decision_id,
        "decision_digest": decision_digest,
        "path": path,
        "capability": {
            "kind": capability.kind,
            "name": capability.name,
            "version": capability.version,
            "content_hash": capability.content_hash,
            "purposes": list(capability.purposes),
        },
        "model_name": model_name,
        "configuration_hash": configuration_hash,
        "timeout_seconds": timeout_seconds,
        "latency_bound_ms": latency_bound_ms,
        "max_output_tokens": max_output_tokens,
        "max_cost_microunits": max_cost_microunits,
        "downgrade_conditions": list(downgrade_conditions),
        "price": {
            "source": price.source,
            "price_version": price.price_version,
            "input_cost_per_token": price.input_cost_per_token,
            "output_cost_per_token": price.output_cost_per_token,
            "currency": price.currency,
        },
        "reservation": {
            "input_token_bound": reservation.input_token_bound,
            "output_token_bound": reservation.output_token_bound,
            "tool_calls": reservation.tool_calls,
            "cost_microunits": reservation.cost_microunits,
            "latency_bound_ms": reservation.latency_bound_ms,
        },
        "observation": _observation_payload(observation),
        "settlement_status": settlement_status,
    }


__all__ = [
    "DOWNGRADE_CONDITIONS",
    "ROUTING_SNAPSHOT_VERSION",
    "ExecutionPath",
    "ObservationSource",
    "PriceSource",
    "RouteCapability",
    "RouteObservation",
    "RoutePrice",
    "RouteReservation",
    "RoutingSnapshot",
    "SettlementStatus",
    "routing_snapshot_payload",
]
