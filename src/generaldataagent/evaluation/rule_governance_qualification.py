"""Evidence and audit checks for the development rule gateway path."""

import json
from pathlib import Path
from uuid import uuid4

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.business.rules.governed import GovernedRuleRunner
from generaldataagent.contracts import ActorContext, ArtifactRef, ToolRequest
from generaldataagent.contracts.rule_tools import RuleAnalysisRequest
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.orchestration.rule_workflow import RuleWorkflowRuntime
from generaldataagent.policy import request_fingerprint


def run_governed_case(
    flow: RuleWorkflowRuntime,
    request: RuleAnalysisRequest,
    actor: ActorContext,
    output: Path,
) -> tuple[str, dict, dict]:
    """Execute through the gateway and verify persisted evidence against the checkpoint."""
    task_id = uuid4()
    trace_id = f"qualification-{task_id}"
    store = LocalArtifactStore(output / "artifacts")
    audit = JsonlAuditSink(output / "audit.jsonl")
    runner = GovernedRuleRunner(runtime=flow, actor=actor, artifacts=store, audit=audit)
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
    result = runner.invoke(proposal, trace_id=trace_id)
    thread = str(task_id)
    state = flow.state_of(thread, request, actor)
    ref = ArtifactRef.model_validate(result["evidence"])
    content = store.read_bytes(ref)  # Recompute hash from bytes, not file presence.
    evidence = json.loads(content)
    expected_evidence = dict(
        task_id=thread,
        thread_id=thread,
        request=request.model_dump(mode="json"),
        source_hashes=state["source_hashes"],
        binding_hash=state["binding_hash"],
        status=state["status"],
        verification=state.get("verification"),
        computation=state.get("computation"),
        delivery=state.get("delivery"),
    )
    artifact_request = ToolRequest(
        task_id=task_id,
        user_id=actor.user_id,
        tenant_id=actor.tenant_id,
        project_id=actor.project_id,
        agent_role="reporter",
        tool_name="artifact.write",
        action="create",
        resource="rule-workflow-evidence",
        data_domains=actor.data_domains,
        arguments=expected_evidence,
    )
    events = [json.loads(line) for line in audit.path.read_text().splitlines()]
    expected_fingerprints = [request_fingerprint(proposal), request_fingerprint(artifact_request)]
    checks = dict(
        task_binding=ref.task_id == task_id and result["thread_id"] == thread,
        artifact_metadata=ref.artifact_type == "rule.workflow.evidence"
        and ref.size_bytes == len(content),
        evidence_matches_checkpoint=evidence == expected_evidence,
        result_matches_checkpoint=result["status"] == state["status"],
        verification_passed=state.get("verification", {}).get("passed") is True,
        development_only=result.get("production_accepted") is False,
        audit_binding=len(events) == 2
        and all(
            event["event_type"] == "policy.decision"
            and event["task_id"] == thread
            and event["actor_id"] == actor.user_id
            and event["tenant_id"] == actor.tenant_id
            and event["trace_id"] == trace_id
            and event["payload"]["decision"] == "allow"
            and event["payload"]["request_fingerprint"] == fingerprint
            for event, fingerprint in zip(events, expected_fingerprints, strict=True)
        ),
    )
    return (
        thread,
        state,
        dict(
            checks=checks,
            passed=all(checks.values()),
            evidence=ref.model_dump(mode="json"),
            audit_event_count=len(events),
            expected_request_fingerprints=expected_fingerprints,
        ),
    )
