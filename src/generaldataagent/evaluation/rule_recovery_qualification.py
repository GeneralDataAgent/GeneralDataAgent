"""Development fault/restart qualification for the authored business requests."""

import hashlib
import json
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.business.rules.governed import GovernedRuleRunner
from generaldataagent.business.rules.payments import SQLitePaymentsSource
from generaldataagent.business.rules.workflow import AnnexWorkflowServices
from generaldataagent.contracts import ActorContext, ArtifactRef, ToolRequest
from generaldataagent.contracts.rule_tools import RuleAnalysisRequest
from generaldataagent.evaluation.rule_business_qualification import _close
from generaldataagent.evaluation.rule_fingerprint import rule_workflow_fingerprint
from generaldataagent.evaluation.rule_qualification import (
    _context_database,
    independent_context_expected,
    independent_expected,
)
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.orchestration.checkpoints import open_protected_checkpointer
from generaldataagent.orchestration.rule_workflow import RuleWorkflowRuntime
from generaldataagent.policy import request_fingerprint


class _EvidenceAuditOutage(OSError):
    pass


class _FailEvidenceAudit(JsonlAuditSink):
    def emit(self, event):
        if event.payload["decision"] == "allow" and getattr(self, "emitted", False):
            raise _EvidenceAuditOutage("synthetic evidence audit outage")
        super().emit(event)
        self.emitted = True


class _EvidenceWriteOutage(OSError):
    pass


def _recover_case(data, case, output, fault="audit"):
    output.mkdir()
    task_id = uuid4()
    actor = ActorContext(
        user_id="qualification",
        tenant_id="synthetic",
        project_id="rules",
        data_domains=("synthetic",),
    )
    request = RuleAnalysisRequest(
        **(
            dict(
                semantics_excerpt=data["semantics_excerpt"],
                empty_list_is_wildcard=case.get("empty_list_is_wildcard", False),
            )
            | case["request"]
        )
    )
    proposal = ToolRequest(
        task_id=task_id,
        user_id=actor.user_id,
        tenant_id=actor.tenant_id,
        project_id=actor.project_id,
        agent_role="data_analyst",
        tool_name="rules.execute",
        action="run",
        resource="rule-workflow",
        data_domains=actor.data_domains,
        arguments=request.model_dump(mode="json"),
    )
    database = output / "payments.sqlite"
    _context_database(database, data["merchant"], data["payments"])
    payments = SQLitePaymentsSource(database, allowed_relations={"merchant_data", "payments"})
    adapter = AnnexWorkflowServices(data["annex"], documents=data["documents"], payments=payments)
    ports = adapter.services()
    counts = {}

    def counted(name, handler):
        def invoke(state):
            counts[name] = counts.get(name, 0) + 1
            return handler(state)

        return invoke

    ports = replace(
        ports,
        **{
            name: counted(name, getattr(ports, name))
            for name in ("monthly_context", "match_rules", "compute", "verify", "deliver")
        },
    )
    store = LocalArtifactStore(output / "artifacts")
    checkpoint = output / "checkpoints.sqlite"
    checks = {}
    key = b"synthetic-rule-qualification-key!"[:32]

    def build(saver, audit):
        flow = RuleWorkflowRuntime(
            services=ports,
            checkpointer=saver,
            source_hashes=adapter.source_hashes,
            workflow_version="authored-rule-recovery-development-v1",
        )
        return flow, GovernedRuleRunner(runtime=flow, actor=actor, artifacts=store, audit=audit)

    with open_protected_checkpointer(checkpoint, encryption_key=key) as saver:
        audit = (
            _FailEvidenceAudit(output / "outage-audit.jsonl")
            if fault == "audit"
            else JsonlAuditSink(output / "outage-audit.jsonl")
        )
        flow, runner = build(saver, audit)
        outage = _EvidenceAuditOutage if fault == "audit" else _EvidenceWriteOutage
        injection = (
            patch.object(store, "put_json", side_effect=outage("synthetic evidence write outage"))
            if fault == "write"
            else nullcontext()
        )
        with injection:
            try:
                runner.invoke(proposal, trace_id="outage")
            except outage:
                checks["expected_outage"] = True
            else:
                checks["expected_outage"] = False
        outage_events = [json.loads(line) for line in audit.path.read_text().splitlines()]
        checks["outage_audit"] = len(outage_events) == (2 if fault == "write" else 1) and all(
            event["task_id"] == str(task_id)
            and event["trace_id"] == "outage"
            and event["payload"]["decision"] == "allow"
            for event in outage_events
        )
        before = flow.state_of(str(task_id), request, actor)
        checks["completed_checkpoint_without_artifact"] = before[
            "status"
        ] == "completed" and not list(store.root.iterdir())
        before_counts = dict(counts)
        expected_fingerprints = [request_fingerprint(proposal)]
        if fault == "write":
            evidence_request = ToolRequest(
                task_id=task_id,
                user_id=actor.user_id,
                tenant_id=actor.tenant_id,
                project_id=actor.project_id,
                agent_role=proposal.agent_role,
                tool_name="artifact.write",
                action="create",
                resource="rule-workflow-evidence",
                data_domains=actor.data_domains,
                arguments={
                    "task_id": str(task_id),
                    "thread_id": str(task_id),
                    **{
                        name: before[name]
                        for name in (
                            "request",
                            "source_hashes",
                            "binding_hash",
                            "status",
                            "verification",
                            "computation",
                            "delivery",
                        )
                    },
                },
            )
            expected_fingerprints.append(request_fingerprint(evidence_request))
        checks["outage_audit_binding"] = [
            event["payload"]["request_fingerprint"] for event in outage_events
        ] == expected_fingerprints

    with open_protected_checkpointer(checkpoint, encryption_key=key) as saver:
        flow, runner = build(saver, JsonlAuditSink(output / "recovery-audit.jsonl"))
        altered = proposal.model_copy(
            update={
                "action": "resume",
                "arguments": request.model_copy(update={"transaction_value": "999"}).model_dump(
                    mode="json"
                ),
            }
        )
        try:
            runner.invoke(altered, trace_id="changed-request")
        except ValueError as exc:
            checks["changed_request_denied"] = str(exc) == "rule_workflow_binding_changed"
        else:
            checks["changed_request_denied"] = False
        try:
            runner.invoke(proposal, trace_id="duplicate-run")
        except PermissionError as exc:
            checks["duplicate_run_denied"] = str(exc) == "thread_already_registered_use_resume"
        else:
            checks["duplicate_run_denied"] = False
        checks["rejected_calls_preserve_checkpoint"] = (
            flow.state_of(str(task_id), request, actor) == before
            and counts == before_counts
            and not list(store.root.iterdir())
        )
        resumed = proposal.model_copy(update={"action": "resume"})
        result = runner.invoke(resumed, trace_id="recovered")
        state = flow.state_of(str(task_id), request, actor)
        ref = ArtifactRef.model_validate(result["evidence"])
        evidence = json.loads(store.read_bytes(ref))
        checks["no_node_replay"] = counts == before_counts and set(counts.values()) == {1}
        checks["checkpoint_unchanged"] = state == before
        checks["task_evidence_binding"] = (
            ref.task_id == task_id
            and result["thread_id"] == str(task_id)
            and evidence["task_id"] == evidence["thread_id"] == str(task_id)
            and evidence["request"] == request.model_dump(mode="json")
            and all(
                evidence[name] == state[name]
                for name in (
                    "source_hashes",
                    "binding_hash",
                    "status",
                    "verification",
                    "computation",
                    "delivery",
                )
            )
        )
        answer = evidence["delivery"]
        if request.operation in {"applicable", "counterfactual"}:
            ids, change = independent_context_expected(
                data["merchant"], data["payments"], data["annex"], request.model_dump()
            )
            matches = (
                all(_close(answer.get(name), value) for name, value in change.items())
                if request.operation == "counterfactual"
                else answer.get("items") == ids
            )
        else:
            ids, mean = independent_expected(data["annex"], request.model_dump())
            matches = answer.get("items") == ids
            if request.operation == "average_fee":
                matches = matches and _close(answer.get("average_fee"), mean)
        checks["independent_answer"] = matches and evidence["verification"]["passed"] is True
        events = [json.loads(line) for line in runner.audit.path.read_text().splitlines()]
        checks["recovery_audit"] = [event["trace_id"] for event in events] == [
            "changed-request",
            "duplicate-run",
            "recovered",
            "recovered",
        ] and all(
            event["task_id"] == str(task_id) and event["payload"]["decision"] == "allow"
            for event in events
        )
    return dict(
        case_id=case["case_id"],
        task_id=str(task_id),
        passed=all(checks.values()),
        checks=checks,
        node_counts=counts,
        evidence=ref.model_dump(mode="json"),
        observed_answer=answer,
        source_hashes=adapter.source_hashes(),
        initial_failure=outage.__name__,
        fault_mode=fault,
    )


def qualify_recovery(corpus: Path, output: Path, *, fault: str = "audit"):
    if fault not in {"audit", "write"}:
        raise ValueError("unknown recovery qualification fault")
    payload = corpus.read_bytes()
    data = json.loads(payload)
    cases = [case for case in data["cases"] if "request" in case]
    if not cases or len({case["case_id"] for case in cases}) != len(cases):
        raise ValueError("recovery qualification requires unique accepted business requests")
    workflow_sources = rule_workflow_fingerprint(governed=True)
    output.mkdir(parents=True, exist_ok=False)
    (output / "corpus.json").write_bytes(payload)
    report = dict(
        suite="authored-rule-recovery-development-v1",
        fault_mode=fault,
        parent_corpus_n=len(data["cases"]),
        corpus_sha256=hashlib.sha256(payload).hexdigest(),
        runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        workflow_source_sha256=workflow_sources,
        planned_case_ids=[case["case_id"] for case in cases],
        n=len(cases),
        results=[],
        run_status="running",
        model_calls=0,
        benchmark_score=False,
        production_accepted=False,
        independent_human_review="not_run",
        unseen_evaluation="not_run",
        limitations=[
            "accepted structured requests only; parser refusals not recovery scenarios",
            "synthetic development evidence outage and in-process database close/reopen",
            "write fault is injected before put_json writes bytes; no partial-file qualification",
            "no model service, process-kill, production audit or release qualification",
        ],
    )

    def persist():
        report.update(
            processed_n=len(report["results"]),
            pending_n=report["n"] - len(report["results"]),
            passed=sum(row["passed"] for row in report["results"]),
        )
        partial = output / "report.partial"
        partial.write_text(json.dumps(report, indent=2) + "\n")
        partial.replace(output / "report.json")

    persist()
    for index, case in enumerate(cases):
        try:
            row = _recover_case(data, case, output / f"case-{index:04d}", fault)
        except Exception as exc:
            row = dict(
                case_id=case["case_id"],
                fault_mode=fault,
                passed=False,
                error_type=type(exc).__name__,
            )
        report["results"].append(row)
        persist()
    report["run_status"] = "completed"
    persist()
    return report
