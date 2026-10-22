"""Executable Stage 0 baseline evidence without any model or network call.

The roadmap's Stage 0 exit asks for three things that unit tests alone do not
demonstrate: a baseline task cohort, checkpoint fault injection, and failed-tool
and approval behaviour with correlated traces. This runner produces one
machine-readable report for all three, so a future regression shows up as a
failing gate rather than a missing document.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from generaldataagent.contracts import (
    ActorContext,
    ArtifactClassification,
    ToolRequest,
)
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.orchestration import PhaseOneServices, build_phase_one_graph
from generaldataagent.orchestration.checkpoints import open_protected_checkpointer
from generaldataagent.policy import PolicyEngine, SQLiteApprovalStore
from generaldataagent.tools.gateway import PolicyDeniedError, ToolGateway

ROOT = Path(__file__).resolve().parents[1]
ACTOR = ActorContext(
    user_id="stage0-user", tenant_id="stage0-tenant", project_id="stage0", data_domains=("stage0",)
)


def baseline_cohort() -> list[dict]:
    """A small deterministic cohort of read-only metric questions."""
    return [
        {
            "case_id": f"metric-{index:02d}",
            "question": f"count of accepted orders grouped by channel, variant {index}",
            "expected_status": "completed",
            "dimension": "channel" if index % 2 else None,
        }
        for index in range(30)
    ]


def run_cohort(artifact_root: Path) -> dict:
    """Execute the cohort through the real Phase 1 graph with a pass gate."""
    from generaldataagent.orchestration import MetricAnalysisRuntime

    database = artifact_root / "stage0-orders.db"
    artifact_root.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE IF EXISTS orders")
        connection.execute("CREATE TABLE orders (created_at TEXT, channel TEXT)")
        connection.executemany(
            "INSERT INTO orders VALUES (?, ?)",
            [
                ("2026-10-01T00:00:00+00:00", "web"),
                ("2026-10-02T00:00:00+00:00", "web"),
                ("2026-10-03T00:00:00+00:00", "store"),
            ],
        )
    from generaldataagent.artifacts import LocalArtifactStore
    from generaldataagent.catalog import InMemoryMetricCatalog, MetricDefinition
    from generaldataagent.contracts import MetricQuery
    from generaldataagent.tools import SQLiteReadOnlyExecutor

    catalog = InMemoryMetricCatalog(
        [
            MetricDefinition(
                name="order_count",
                version="v1",
                description="Number of accepted orders",
                data_domain="stage0",
                relation="orders",
                expression="COUNT(*)",
                time_column="created_at",
                allowed_dimensions=("channel",),
                owner="stage0-team",
            )
        ]
    )
    runtime = MetricAnalysisRuntime(
        catalog=catalog,
        executor=SQLiteReadOnlyExecutor(database),
        artifacts=LocalArtifactStore(artifact_root / "artifacts"),
        audit=JsonlAuditSink(artifact_root / "audit.jsonl"),
    )
    cases = []
    for case in baseline_cohort():
        query = MetricQuery(
            metric_name="order_count",
            metric_version="v1",
            dimensions=[case["dimension"]] if case["dimension"] else [],
            start_time=datetime(2026, 10, 1, tzinfo=UTC),
            end_time=datetime(2026, 11, 1, tzinfo=UTC),
        )
        result = runtime.run(query, ACTOR)
        passed = result.get("status") == case["expected_status"]
        cases.append(
            {
                "case_id": case["case_id"],
                "status": result.get("status"),
                "passed": passed,
                "artifact_count": len(result.get("delivery_package", {}).get("evidence", [])),
            }
        )
    return {
        "total": len(cases),
        "passed": sum(1 for case in cases if case["passed"]),
        "cases": cases,
    }


def run_checkpoint_faults(artifact_root: Path, rounds: int = 20) -> dict:
    """Interrupt at each graph stage, restart, and prove no stage replays."""
    checkpoint_root = artifact_root / "checkpoints"
    key = b"stage0-fault-injection-key".ljust(32, b"0")  # exactly 32 bytes
    stages = ("discover", "plan", "analyze", "validate")
    invocations: dict[str, dict[str, int]] = {}
    outcomes = []

    def counter(name):
        def handler(state):
            bucket = invocations.setdefault(state["task_id"], {})
            bucket[name] = bucket.get(name, 0) + 1
            return _stage_result(name)

        return handler

    services = PhaseOneServices(
        discover=counter("discover"),
        plan=counter("plan"),
        analyze=counter("analyze"),
        validate=counter("validate"),
        deliver=lambda state: {"status": "completed"},
    )
    for index in range(rounds):
        thread = str(uuid4())
        config = {"configurable": {"thread_id": thread}}
        interrupt_after = [stages[index % len(stages)]]
        state = {
            "task_id": thread,
            "actor_context": ACTOR.model_dump(mode="json"),
            "user_query": "stage0 fault injection",
        }
        with open_protected_checkpointer(checkpoint_root, encryption_key=key) as saver:
            saver.register_thread(thread, ACTOR)
            graph = build_phase_one_graph(services, checkpointer=saver)
            graph.invoke(state, config, interrupt_after=interrupt_after)
            interrupted = bool(graph.get_state(config).next)
        with open_protected_checkpointer(checkpoint_root, encryption_key=key) as saver:
            saver.authorize_thread(thread, ACTOR)
            graph = build_phase_one_graph(services, checkpointer=saver)
            final = graph.invoke(None, config)
        counts = invocations[thread]
        # Stages completed before the interrupt must never run twice.
        index_of_interrupt = stages.index(interrupt_after[0])
        replayed = [stage for stage in stages[: index_of_interrupt + 1] if counts.get(stage) != 1]
        outcomes.append(
            {
                "round": index,
                "interrupted_after": interrupt_after[0],
                "was_interrupted": interrupted,
                "status": final.get("status"),
                "replayed_stages": replayed,
                "passed": interrupted and final.get("status") == "completed" and not replayed,
            }
        )
    return {
        "total": len(outcomes),
        "passed": sum(1 for item in outcomes if item["passed"]),
        "cases": outcomes,
    }


def _stage_result(name: str) -> dict:
    return {
        "discover": {"semantic_context": {"metric": "order_count"}},
        "plan": {"task_plan": {"steps": ["analyze"]}},
        "analyze": {"analysis_result": {"rows": 1}},
        "validate": {"validation_result": {"status": "pass"}},
    }[name]


def run_failure_and_approval(artifact_root: Path) -> dict:
    """Prove denial, approval-gated execution, replay blocking and trace correlation."""
    audit_path = artifact_root / "fault-audit.jsonl"
    audit = JsonlAuditSink(audit_path)
    approvals = SQLiteApprovalStore(artifact_root / "fault-approvals.db")
    gateway = ToolGateway(PolicyEngine(), audit, approvals=approvals)
    executed = []
    gateway.register(
        "development.write", lambda arguments: executed.append(arguments) or {"ok": True}
    )

    def request(action: str, risk: str) -> ToolRequest:
        return ToolRequest(
            task_id=uuid4(),
            user_id=ACTOR.user_id,
            tenant_id=ACTOR.tenant_id,
            project_id=ACTOR.project_id,
            data_domains=ACTOR.data_domains,
            tool_name="development.write",
            action=action,
            resource="dev.orders",
            agent_role="data_engineer",
            requested_risk=risk,
            arguments={"target": "dev_orders"},
        )

    checks = []
    denied_request = request("materialize", "r2")
    try:
        gateway.invoke(denied_request, trace_id="trace-denied")
        denied = False
    except PolicyDeniedError:
        denied = True
    checks.append(
        {
            "name": "unapproved_write_is_denied",
            "passed": denied and not executed,
            "trace_id": "trace-denied",
        }
    )

    granted_request = request("materialize", "r2")
    approval = approvals.create_request(
        granted_request,
        impact_scope="stage0 synthetic development table",
        cost_estimate="no external cost",
        classification=ArtifactClassification.INTERNAL,
        rollback_plan="drop dev_orders",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    approvals.approve(approval.approval_id, approver_id="independent-approver")
    gateway.invoke(granted_request, trace_id="trace-approved", approval_id=approval.approval_id)
    checks.append(
        {
            "name": "approved_write_executes_once",
            "passed": executed == [{"target": "dev_orders"}],
            "trace_id": "trace-approved",
        }
    )

    try:
        gateway.invoke(granted_request, trace_id="trace-replay", approval_id=approval.approval_id)
        replay_blocked = False
    except Exception:
        replay_blocked = True
    checks.append(
        {
            "name": "approval_replay_is_blocked",
            "passed": replay_blocked and len(executed) == 1,
            "trace_id": "trace-replay",
        }
    )

    events = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    traces = {event["trace_id"] for event in events}
    approved_events = [event for event in events if event["trace_id"] == "trace-approved"]
    checks.append(
        {
            "name": "audit_traces_cover_every_invocation",
            "passed": {"trace-denied", "trace-approved", "trace-replay"} <= traces,
            "traces": sorted(traces),
        }
    )
    checks.append(
        {
            "name": "approved_trace_has_decision_and_consumption",
            "passed": {event["event_type"] for event in approved_events}
            >= {"policy.decision", "approval.consumed"},
            "event_types": sorted({event["event_type"] for event in approved_events}),
        }
    )
    return {"checks": checks, "passed": all(check["passed"] for check in checks)}


def _uuid(value: str | UUID) -> str:
    return str(value)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / ".artifacts/stage0/baseline")
    args = parser.parse_args()
    output = args.output
    output.mkdir(parents=True, exist_ok=True)

    cohort = run_cohort(output)
    faults = run_checkpoint_faults(output)
    failures = run_failure_and_approval(output)
    report = {
        "baseline_cohort": cohort,
        "checkpoint_fault_injection": faults,
        "failure_and_approval": failures,
        "model_calls": 0,
        "passed": (
            cohort["passed"] == cohort["total"]
            and faults["passed"] == faults["total"]
            and failures["passed"]
        ),
    }
    (output / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, default=_uuid), encoding="utf-8"
    )
    print(json.dumps({key: report[key] for key in ("passed", "model_calls")}, sort_keys=True))
    if not report["passed"]:
        print(
            json.dumps(
                {
                    "cohort": f"{cohort['passed']}/{cohort['total']}",
                    "faults": f"{faults['passed']}/{faults['total']}",
                    "failures": [c for c in failures["checks"] if not c["passed"]],
                },
                sort_keys=True,
            )
        )
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
