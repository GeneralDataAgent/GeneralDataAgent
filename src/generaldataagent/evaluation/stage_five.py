"""Frozen local action lifecycle matrix. Local draft success is never external acceptance."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, TypeAdapter

from generaldataagent.business.actions import ActionControl, BusinessActionRuntime
from generaldataagent.contracts.business import (
    ActionIntent,
    DashboardDraft,
    NotificationDraft,
    RegistrationDraft,
    ReportDraft,
    SchedulePRDraft,
    TicketDraft,
)
from generaldataagent.contracts.models import ContractModel
from generaldataagent.policy.approvals import SQLiteApprovalStore
from generaldataagent.security.identity import SessionBroker

KINDS = ("dashboard", "ticket", "schedule_pr", "model_registration", "notification", "report")
SCENARIOS = ("approved", "unapproved", "cancelled", "expired", "retry", "compensation", "disabled")


class BusinessCase(ContractModel):
    case_id: str = Field(pattern=r"^stage5_[0-9]{3}$")
    kind: Literal[
        "dashboard", "ticket", "schedule_pr", "model_registration", "notification", "report"
    ]
    scenario: Literal[
        "approved", "unapproved", "cancelled", "expired", "retry", "compensation", "disabled"
    ]
    expected: Literal["completed", "rejected"]


def _payload(kind):
    digest = "sha256:" + "a" * 64
    return {
        "dashboard": lambda: DashboardDraft(
            title="Fixture", metric_names=("revenue",), evidence_hashes=(digest,)
        ),
        "ticket": lambda: TicketDraft(title="Fixture", description="Review source quality"),
        "schedule_pr": lambda: SchedulePRDraft(
            pipeline="daily",
            interval_minutes=60,
            rollback_interval_minutes=1440,
            rationale="Fixture",
        ),
        "model_registration": lambda: RegistrationDraft(
            experiment_id=UUID(int=1), model_card_hash=digest
        ),
        "notification": lambda: NotificationDraft(message="Fixture ready", evidence_hash=digest),
        "report": lambda: ReportDraft(title="Fixture", report_hash=digest, period="2026-W42"),
    }[kind]()


def _run_case(case, path):
    from dataclasses import replace

    path.mkdir()
    broker = SessionBroker()
    approvals = SQLiteApprovalStore(path / "approvals")
    control = ActionControl(case.kind, "fixture", "owner", "t", "p", frozenset({"d"}), enabled=True)
    actions = BusinessActionRuntime(
        path / "actions", broker=broker, approvals=approvals, controls=(control,)
    )
    token = broker.issue(
        user_id="requester",
        tenant_id="t",
        project_id="p",
        data_domains=frozenset({"d"}),
        permissions=frozenset({"business:propose", "business:read", "actions:resume"}),
    )
    intent = ActionIntent(
        task_id=uuid4(),
        thread_id="fixture-thread",
        idempotency_key="fixture",
        destination="fixture",
        payload=_payload(case.kind),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    proposed = actions.propose(intent, token)
    if case.scenario != "unapproved":
        approvals.approve(UUID(proposed["approval_id"]), approver_id="independent-fixture-reviewer")
    if case.scenario == "cancelled":
        actions.cancel(intent.action_id, token)
    elif case.scenario == "expired":
        actions.clock = lambda: datetime.now(UTC) + timedelta(hours=1)
    elif case.scenario == "disabled":
        actions.controls[(case.kind, "fixture")] = replace(control, enabled=False)
    try:
        result = actions.execute(intent.action_id, token)
    except (PermissionError, ValueError):
        with actions._db() as db:
            no_effect = db.execute("SELECT COUNT(*) FROM business_resources").fetchone()[0] == 0
        return "rejected" if no_effect and actions.verify_audit() else "unsafe_effect"
    if case.scenario == "retry":
        restarted = BusinessActionRuntime(
            actions.path, broker=broker, approvals=approvals, controls=(control,)
        )
        if restarted.execute(intent.action_id, token) != result:
            return "duplicate_or_changed_receipt"
    if case.scenario == "compensation":
        compensation = ActionIntent(
            task_id=intent.task_id,
            thread_id=intent.thread_id,
            idempotency_key="compensation",
            destination="fixture",
            payload=intent.payload,
            operation="compensate",
            compensates=intent.action_id,
            expected_revision=1,
            expires_at=intent.expires_at,
        )
        proposed = actions.propose(compensation, token)
        approvals.approve(UUID(proposed["approval_id"]), approver_id="independent-fixture-reviewer")
        result = actions.execute(compensation.action_id, token)
        with actions._db() as db:
            if db.execute("SELECT active FROM business_resources").fetchone()[0] != 0:
                return "compensation_failed"
    if result["receipt"]["external_delivery"] or not actions.verify_audit():
        return "invalid_receipt_or_audit"
    return "completed"


def run_business_regression(path: Path) -> dict:
    raw = path.read_bytes()
    cases = TypeAdapter(list[BusinessCase]).validate_python(
        [json.loads(line) for line in raw.splitlines() if line.strip()]
    )
    expected_pairs = {(kind, scenario) for kind in KINDS for scenario in SCENARIOS}
    if (
        len(cases) != len(expected_pairs)
        or len({c.case_id for c in cases}) != len(cases)
        or {(c.kind, c.scenario) for c in cases} != expected_pairs
    ):
        raise ValueError("Stage 5 requires a complete uniquely identified action/scenario matrix")
    results = []
    with TemporaryDirectory(prefix="gda-stage5-evaluation-") as work:
        for case in cases:
            try:
                actual = _run_case(case, Path(work) / case.case_id)
            except Exception:
                actual = "unexpected_error"
            results.append(
                {
                    "case_id": case.case_id,
                    "kind": case.kind,
                    "scenario": case.scenario,
                    "expected": case.expected,
                    "actual": actual,
                    "passed": actual == case.expected,
                }
            )
    passed = sum(r["passed"] for r in results)
    return {
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "total": len(cases),
        "passed": passed,
        "failed": len(cases) - passed,
        "cases": results,
        "backend": "local_draft",
        "external_delivery": False,
    }
