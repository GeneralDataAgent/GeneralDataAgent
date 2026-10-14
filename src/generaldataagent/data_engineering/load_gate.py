"""Reproducible local load gate for governed parallel profiling."""

from math import ceil
from time import monotonic
from uuid import uuid4

from generaldataagent.contracts import ActorContext
from generaldataagent.contracts.data_engineering import (
    ProfileBatchRequest,
    ProfileLoadGateResult,
)
from generaldataagent.data_engineering.runtime import DataEngineeringRuntime


def run_profile_load_gate(
    runtime: DataEngineeringRuntime,
    *,
    actor: ActorContext,
    connector_id: str,
    relation: str,
    request_count: int = 100,
    max_workers: int = 8,
    max_p95_ms: float = 5_000.0,
    trace_id: str = "stage2-load-gate",
) -> ProfileLoadGateResult:
    """Run identical isolated profile requests and evaluate a strict local gate."""
    if request_count < 1 or request_count > 10_000:
        raise ValueError("request_count must be between 1 and 10000")
    if max_p95_ms <= 0:
        raise ValueError("max_p95_ms must be positive")

    before_artifacts = len(tuple(runtime.artifacts.root.rglob("*.json")))
    requests = tuple(
        ProfileBatchRequest(
            task_id=uuid4(),
            connector_id=connector_id,
            relation=relation,
        )
        for _ in range(request_count)
    )
    started_at = monotonic()
    batch = runtime.profile_relations_parallel(
        actor=actor,
        requests=requests,
        trace_id=trace_id,
        max_workers=max_workers,
    )
    elapsed_ms = (monotonic() - started_at) * 1_000
    after_artifacts = len(tuple(runtime.artifacts.root.rglob("*.json")))
    latencies = sorted(item.elapsed_ms for item in batch.items)
    p95_index = max(0, ceil(len(latencies) * 0.95) - 1)
    succeeded = sum(item.status == "succeeded" for item in batch.items)
    failed = sum(item.status == "failed" for item in batch.items)
    cancelled = sum(item.status == "cancelled" for item in batch.items)
    artifact_count = after_artifacts - before_artifacts
    p95_request_ms = latencies[p95_index]
    passed = (
        failed == 0
        and cancelled == 0
        and succeeded == request_count
        and artifact_count == succeeded
        and p95_request_ms <= max_p95_ms
    )
    return ProfileLoadGateResult(
        request_count=request_count,
        max_workers=max_workers,
        succeeded=succeeded,
        failed=failed,
        cancelled=cancelled,
        artifact_count=artifact_count,
        elapsed_ms=elapsed_ms,
        p95_request_ms=p95_request_ms,
        max_p95_ms=max_p95_ms,
        passed=passed,
    )
