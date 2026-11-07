"""Stage 15.1A: digest-bound product routing snapshots and fail-closed unknowns."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError
from test_metric_analysis import actor as metric_actor
from test_metric_analysis import query
from test_stage5_routed_analysis import routed_runtime

from generaldataagent.business.governance import Capability, CapabilityRegistry
from generaldataagent.business.optimization import (
    GovernedModelExecutor,
    ModelRoute,
    ProviderResult,
    RoutingRequest,
    ScopedResponseCache,
)
from generaldataagent.contracts import ActorContext, ArtifactRef, Budget
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
from generaldataagent.policy.budgets import TaskBudgetLedger
from generaldataagent.routing.snapshot import (
    RouteSnapshotRefused,
    RouteSnapshotSpec,
    RouteSnapshotTracker,
    settled_observation,
)

HASH = "sha256:" + "a" * 64
ACTOR = ActorContext(
    user_id="user-1",
    tenant_id="tenant-1",
    project_id="project-1",
    data_domains=("commerce",),
)


def digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def capability(**kwargs) -> RouteCapability:
    values = dict(
        kind="model",
        name="narrator",
        version="v1",
        content_hash=HASH,
        purposes=("narrative",),
    )
    values.update(kwargs)
    return RouteCapability(**values)


def spec(**kwargs) -> RouteSnapshotSpec:
    values = dict(
        capability=capability(),
        model_name="narrator",
        configuration_hash=HASH,
        price_version="prices-2026-11",
        price_known=True,
        timeout_seconds=1,
        tool_calls=1,
    )
    values.update(kwargs)
    return RouteSnapshotSpec(**values)


def request(**kwargs) -> RoutingRequest:
    values = dict(
        purpose="narrative",
        prompt="Explain the certified result",
        source_hash=HASH,
        prompt_version="v1",
        policy_version="v1",
        authorization_version="v1",
        classification="internal",
        input_token_bound=20,
        output_token_bound=10,
        max_cost_microunits=1000,
        max_latency_ms=1000,
    )
    values.update(kwargs)
    return RoutingRequest(**values)


def route(**kwargs) -> ModelRoute:
    values = dict(
        name="narrator",
        version="v1",
        content_hash=HASH,
        purposes=frozenset({"narrative"}),
        classifications=frozenset({"internal"}),
        input_cost_per_token=1,
        output_cost_per_token=2,
        estimated_latency_ms=100,
        enabled=True,
    )
    values.update(kwargs)
    return ModelRoute(**values)


def tracked(**kwargs):
    task = kwargs.pop("task_id", uuid4())
    decision = kwargs.pop("decision_id", "narrator-call")
    current_spec = kwargs.pop("spec", spec())
    current_route = kwargs.pop("route", route())
    current_request = kwargs.pop("request", request())
    return RouteSnapshotTracker.create(
        task_id=task,
        decision_id=decision,
        spec=current_spec,
        route=current_route,
        request=current_request,
    )


def snapshot_for(**kwargs) -> RoutingSnapshot:
    tracker = kwargs.pop("tracker", None) or tracked()
    result = kwargs.pop("result", ProviderResult(text="done", input_tokens=15, output_tokens=5))
    return tracker.snapshot(
        reserved_cost_microunits=kwargs.pop("reserved_cost_microunits", 40),
        result=result,
        elapsed_ms=kwargs.pop("elapsed_ms", 100),
        cached=kwargs.pop("cached", False),
        **kwargs,
    )


def payload_for(snapshot: RoutingSnapshot) -> dict:
    return routing_snapshot_payload(
        task_id=snapshot.task_id,
        decision_id=snapshot.decision_id,
        decision_digest=snapshot.decision_digest,
        path=snapshot.path,
        capability=snapshot.capability,
        model_name=snapshot.model_name,
        configuration_hash=snapshot.configuration_hash,
        timeout_seconds=snapshot.timeout_seconds,
        latency_bound_ms=snapshot.latency_bound_ms,
        max_output_tokens=snapshot.max_output_tokens,
        max_cost_microunits=snapshot.max_cost_microunits,
        downgrade_conditions=snapshot.downgrade_conditions,
        price=snapshot.price,
        reservation=snapshot.reservation,
        observation=snapshot.observation,
        settlement_status=snapshot.settlement_status,
    )


def rebuilt(snapshot: RoutingSnapshot, /, **changes) -> RoutingSnapshot:
    """Re-run all validators; model_copy() alone deliberately skips them."""
    body = snapshot.model_dump(mode="json")
    for field, value in changes.items():
        if isinstance(value, RouteCapability | RouteObservation | RoutePrice | RouteReservation):
            body[field] = value.model_dump(mode="json")
        else:
            body[field] = value
    if "snapshot_digest" not in changes and any(field != "snapshot_digest" for field in changes):
        body["snapshot_digest"] = digest("tampered")
    return RoutingSnapshot.model_validate(body)


def admitted_registry(tmp_path, current_route: ModelRoute) -> CapabilityRegistry:
    registry = CapabilityRegistry(tmp_path / "admissions", clock=lambda: datetime.now(UTC))
    record = Capability(
        name=current_route.name,
        version=current_route.version,
        kind="model",
        content_hash=current_route.content_hash,
        owner="operator",
        tenant_id=ACTOR.tenant_id,
        project_id=ACTOR.project_id,
        data_domains=ACTOR.data_domains,
        regression_evidence_hash=HASH,
        security_evidence_hash=HASH,
        rollback_version="disabled",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    registry.propose(record)
    for reviewer, discipline in (("q", "quality"), ("s", "security")):
        registry.review(record.name, record.version, reviewer_id=reviewer, discipline=discipline)
    registry.activate(record.name, record.version)
    return registry


def executor_for(tmp_path, *, current_route=None, current_spec=None):
    current_route = current_route or route()
    registry = admitted_registry(tmp_path, current_route)
    budgets = TaskBudgetLedger(tmp_path / "budgets")
    task = uuid4()
    budgets.register(task, ACTOR, Budget(max_tokens=1000), max_cost_microunits=1000)
    calls = []

    async def provider(req):
        calls.append(req)
        return ProviderResult(text="Certified result explanation", input_tokens=15, output_tokens=5)

    executor = GovernedModelExecutor(
        routes=(current_route,),
        registry=registry,
        budgets=budgets,
        providers={(current_route.name, current_route.version): provider},
        authorized=lambda a, req: a == ACTOR,
        cache=ScopedResponseCache(
            tmp_path / "cache", key=Fernet.generate_key(), clock=lambda: datetime.now(UTC)
        ),
    )
    if current_spec is not None:
        executor.snapshot_specs[(current_route.name, current_route.version)] = current_spec
    return executor, task, current_route, calls


def spec_for_route(current_route=None) -> RouteSnapshotSpec:
    current_route = current_route or route()
    return spec(capability=capability(content_hash=current_route.content_hash))


def test_snapshot_records_pinned_route_and_provider_reported_usage():
    snapshot = snapshot_for()
    assert snapshot.path == "admitted_model"
    assert snapshot.capability.content_hash == HASH
    assert snapshot.configuration_hash == HASH
    assert snapshot.price.source == "pinned_operator_table"
    assert snapshot.price.price_version == "prices-2026-11"
    assert snapshot.price.input_cost_per_token == 1
    assert snapshot.downgrade_conditions == DOWNGRADE_CONDITIONS
    assert snapshot.reservation.cost_microunits == 40
    assert snapshot.observation.source == "provider_reported"
    assert snapshot.observation.cost_microunits == 25
    assert snapshot.settlement_status == "settled"
    assert snapshot.snapshot_digest == canonical_hash(payload_for(snapshot))
    assert snapshot.model_dump(mode="json")["snapshot_digest"] == snapshot.snapshot_digest
    assert json.dumps(snapshot.model_dump(mode="json"), sort_keys=True)


def test_cache_hit_and_failed_call_are_unknown_not_zero_usage():
    cached = snapshot_for(result=None, cached=True, reserved_cost_microunits=0, elapsed_ms=None)
    assert cached.observation == RouteObservation(source="unknown", complete=False)
    assert cached.observation.cost_microunits is None
    assert cached.reservation.cost_microunits == 0
    assert cached.settlement_status == "unknown"
    with pytest.raises(RouteSnapshotRefused, match="settlement_unexpected"):
        snapshot_for(result=None, cached=False, elapsed_ms=100)
    with pytest.raises(RouteSnapshotRefused, match="usage_incomplete"):
        snapshot_for(elapsed_ms=None)
    with pytest.raises(RouteSnapshotRefused, match="settlement_state_invalid"):
        snapshot_for(cached="yes")


def test_unpriced_route_refuses_decision_and_unknown_price_cannot_quote():
    with pytest.raises(RouteSnapshotRefused, match="price_version_unexpected"):
        spec(price_known=False, price_version="prices-2026-11")
    unknown_price = RoutePrice(source="unknown", price_version="unknown")
    assert unknown_price.input_cost_per_token is None
    with pytest.raises(ValidationError, match="unknown_price_declared"):
        RoutePrice(source="unknown", price_version="unknown", input_cost_per_token=0)
    with pytest.raises(ValidationError, match="pinned_price_incomplete"):
        RoutePrice(source="pinned_operator_table", price_version="p1")
    missing = spec(price_known=False, price_version="unknown")
    with pytest.raises(RouteSnapshotRefused, match="price_missing"):
        tracked(spec=missing)
    with pytest.raises(RouteSnapshotRefused, match="price_version_missing"):
        spec(price_known=True, price_version="unknown")
    with pytest.raises(RouteSnapshotRefused, match="price_state_invalid"):
        spec(price_known=1)
    with pytest.raises(RouteSnapshotRefused, match="capability_pin_invalid"):
        spec(capability="not-a-capability")


def test_changed_route_pin_purpose_classification_timeout_and_budget_are_refused():
    for change in (
        {"route": route(name="other")},
        {"route": route(version="v2")},
        {"route": route(content_hash=digest("changed"))},
        {"request": request(purpose="science")},
        {"request": request(classification="public")},
        {"request": request(max_latency_ms=2000)},
        {"route": route(estimated_latency_ms=2000)},
        {"route": route(enabled=False)},
        {"request": request(max_cost_microunits=1)},
        {"decision_id": "Invalid Decision"},
    ):
        with pytest.raises(RouteSnapshotRefused):
            tracked(**change)
    with pytest.raises(RouteSnapshotRefused, match="route_pin_mismatch"):
        tracked(spec=spec(capability=capability(version="v2")))
    with pytest.raises(RouteSnapshotRefused, match="input_invalid"):
        tracked(spec=object())


def test_spec_rejects_unreviewed_identity_or_price_values():
    for change in (
        {"configuration_hash": digest("changed")},
        {"model_name": "other"},
        {"timeout_seconds": 0},
        {"tool_calls": 0},
        {"price_version": ""},
    ):
        with pytest.raises(RouteSnapshotRefused):
            spec(**change)


def test_digest_tamper_and_changed_fields_are_rejected():
    snapshot = snapshot_for()
    for change in (
        {"timeout_seconds": 2},
        {"snapshot_digest": digest("other")},
        {"decision_digest": digest("other")},
    ):
        with pytest.raises(ValidationError, match="(digest_mismatch|timeout_mismatch)"):
            rebuilt(snapshot, **change)


@pytest.mark.parametrize(
    "change,match",
    [
        ({"max_output_tokens": None}, "model_identity_incomplete"),
        ({"model_name": None}, "model_identity_incomplete"),
        ({"configuration_hash": None}, "model_identity_incomplete"),
        ({"downgrade_conditions": ()}, "downgrade_conditions_invalid"),
        ({"settlement_status": "unknown"}, "settlement_mismatch"),
    ],
)
def test_contract_rejects_incomplete_or_inconsistent_snapshot(change, match):
    with pytest.raises(ValidationError, match=match):
        rebuilt(snapshot_for(), **change)


def test_unknown_observation_cannot_declare_values_or_completeness():
    with pytest.raises(ValidationError, match="unknown_usage_declared"):
        RouteObservation(source="unknown", input_tokens=0)
    with pytest.raises(ValidationError, match="unknown_usage_declared"):
        RouteObservation(source="unknown", complete=True)
    with pytest.raises(ValidationError, match="provider_usage_incomplete"):
        RouteObservation(source="provider_reported", complete=True)


def test_provider_measurements_cannot_exceed_reserved_bounds():
    snapshot = snapshot_for()
    for field, value in (
        ("input_tokens", 21),
        ("output_tokens", 11),
        ("tool_calls", 2),
        ("elapsed_ms", 1001),
        ("cost_microunits", 41),
    ):
        body = snapshot.model_dump(mode="json")
        body["observation"][field] = value
        body["snapshot_digest"] = canonical_hash(
            payload_for(snapshot) | {"observation": body["observation"]}
        )
        with pytest.raises(ValidationError, match="observed_"):
            RoutingSnapshot.model_validate(body)
    with pytest.raises(RouteSnapshotRefused, match="observed_cost_exceeded"):
        settled_observation(
            route(),
            ProviderResult(text="x", input_tokens=100, output_tokens=0),
            elapsed_ms=1,
            cost_microunits=1,
        )


def test_contract_rejects_changed_unit_price_or_unknown_price_reservation():
    snapshot = snapshot_for()
    changed_price = RoutePrice(
        source="pinned_operator_table",
        price_version="prices-2026-12",
        input_cost_per_token=2,
        output_cost_per_token=2,
    )
    for changes in (
        {"price": changed_price},
        {
            "reservation": RouteReservation(
                **(snapshot.reservation.model_dump() | {"cost_microunits": 39})
            )
        },
        {"price": RoutePrice(source="unknown", price_version="unknown")},
    ):
        with pytest.raises(ValidationError, match="(price|reservation|unpriced)"):
            rebuilt(snapshot, **changes)
    with pytest.raises(ValidationError, match="unknown_price_declared"):
        RoutePrice(source="unknown", price_version="unknown", output_cost_per_token=0)


def test_non_model_paths_require_capability_kind_and_have_no_model_identity():
    snapshot = snapshot_for()
    for path, kind in (("certified_metric", "fixed_tool"), ("fixed_science_tool", "tool")):
        body = snapshot.model_dump(mode="json")
        body.update(
            path=path,
            model_name=None,
            configuration_hash=None,
            max_output_tokens=None,
            observation={"source": "unknown", "complete": False},
            settlement_status="unknown",
            reservation={
                **body["reservation"],
                "cost_microunits": 0,
                "output_token_bound": 1,
            },
            price={"source": "unknown", "price_version": "unknown"},
        )
        body["capability"]["kind"] = kind
        body["snapshot_digest"] = canonical_hash(
            routing_snapshot_payload(
                task_id=snapshot.task_id,
                decision_id=snapshot.decision_id,
                decision_digest=snapshot.decision_digest,
                path=path,
                capability=RouteCapability.model_validate(body["capability"]),
                model_name=None,
                configuration_hash=None,
                timeout_seconds=body["timeout_seconds"],
                latency_bound_ms=body["latency_bound_ms"],
                max_output_tokens=None,
                max_cost_microunits=body["max_cost_microunits"],
                downgrade_conditions=snapshot.downgrade_conditions,
                price=RoutePrice.model_validate(body["price"]),
                reservation=RouteReservation.model_validate(body["reservation"]),
                observation=RouteObservation.model_validate(body["observation"]),
                settlement_status="unknown",
            )
        )
        declaration = RoutingSnapshot.model_validate(body)
        assert declaration.path == path
        assert declaration.capability.kind == kind
        assert declaration.settlement_status == "unknown"
        with pytest.raises(ValidationError, match="capability_invalid"):
            rebuilt(declaration, capability=capability())


def test_executor_emits_snapshot_only_when_reviewed_spec_is_enrolled(tmp_path):
    executor, task, current_route, calls = executor_for(tmp_path)
    plain = executor.execute(task, ACTOR, request())
    assert plain["routing_snapshot"] is None
    assert len(calls) == 1
    executor.snapshot_specs[(current_route.name, current_route.version)] = spec_for_route(
        current_route
    )
    enabled = executor.execute(task, ACTOR, request(prompt="A distinct prompt"))
    assert len(calls) == 2
    declaration = RoutingSnapshot.model_validate(enabled["routing_snapshot"])
    assert declaration.task_id == task
    assert declaration.settlement_status == "settled"
    assert declaration.observation.input_tokens == 15
    assert declaration.decision_digest != digest("other")


def test_executor_cache_hit_keeps_usage_unknown_and_preserves_snapshot_digest(tmp_path):
    executor, task, current_route, _ = executor_for(tmp_path)
    executor.snapshot_specs[(current_route.name, current_route.version)] = spec_for_route(
        current_route
    )
    first = executor.execute(task, ACTOR, request())
    second = executor.execute(task, ACTOR, request())
    assert first["cached"] is False and second["cached"] is True
    assert first["routing_snapshot"]["settlement_status"] == "settled"
    cached = RoutingSnapshot.model_validate(second["routing_snapshot"])
    assert cached.settlement_status == "unknown"
    assert cached.observation.cost_microunits is None
    assert cached.decision_digest == first["routing_snapshot"]["decision_digest"]
    assert cached.snapshot_digest != first["routing_snapshot"]["snapshot_digest"]
    assert cached.snapshot_digest == canonical_hash(payload_for(cached))


def test_metric_workflow_persists_same_task_snapshot_as_evidence(tmp_path):
    runtime, calls, _ = routed_runtime(tmp_path)
    executor = runtime.routed_narrator.executor
    current_route = executor.routes[0]
    runtime.routed_narrator.settings = runtime.routed_narrator.settings.model_copy(
        update={"max_latency_ms": 1000}
    )
    executor.snapshot_specs[(current_route.name, current_route.version)] = RouteSnapshotSpec(
        capability=RouteCapability(
            kind="model",
            name=current_route.name,
            version=current_route.version,
            content_hash=current_route.content_hash,
            purposes=("narrative",),
        ),
        model_name=current_route.name,
        configuration_hash=current_route.content_hash,
        price_version="prices-2026-11",
        price_known=True,
        timeout_seconds=1,
        tool_calls=1,
    )
    result = runtime.run(query(), metric_actor())
    assert result["status"] == "completed"
    assert len(calls) == 1
    snapshot = RoutingSnapshot.model_validate(result["analysis_result"]["routing_snapshot"])
    artifact = result["analysis_result"]["routing_snapshot_artifact"]
    assert snapshot.task_id == UUID(result["task_id"])
    assert artifact["task_id"] == result["task_id"]
    assert artifact["artifact_type"] == "routing_snapshot"
    delivery = result["delivery_package"]
    assert artifact in delivery["evidence"]
    stored = json.loads(runtime.artifacts.read_bytes(ArtifactRef.model_validate(artifact)))
    assert stored == snapshot.model_dump(mode="json")


def test_metric_workflow_keeps_default_consumer_off_when_no_spec_is_enrolled(tmp_path):
    runtime, _, _ = routed_runtime(tmp_path)
    default = runtime.run(query(), metric_actor())
    assert default["status"] == "completed"
    assert "routing_snapshot" not in default["analysis_result"]
    assert len(default["delivery_package"]["evidence"]) == 3


def test_metric_workflow_refuses_a_foreign_task_snapshot(tmp_path):
    runtime, _, _ = routed_runtime(tmp_path)
    executor = runtime.routed_narrator.executor
    current_route = executor.routes[0]
    runtime.routed_narrator.settings = runtime.routed_narrator.settings.model_copy(
        update={"max_latency_ms": 1000}
    )
    executor.snapshot_specs[(current_route.name, current_route.version)] = RouteSnapshotSpec(
        capability=RouteCapability(
            kind="model",
            name=current_route.name,
            version=current_route.version,
            content_hash=current_route.content_hash,
            purposes=("narrative",),
        ),
        model_name=current_route.name,
        configuration_hash=current_route.content_hash,
        price_version="prices-2026-11",
        price_known=True,
        timeout_seconds=1,
        tool_calls=1,
    )
    original_generate = runtime.routed_narrator.generate

    def foreign_snapshot(*args, **kwargs):
        narrative, usage = original_generate(*args, **kwargs)
        foreign = snapshot_for().model_dump(mode="json")
        return narrative, usage | {"routing_snapshot": foreign}

    runtime.routed_narrator.generate = foreign_snapshot
    with pytest.raises(ValueError, match="routing_snapshot_task_mismatch"):
        runtime.run(query(), metric_actor())
