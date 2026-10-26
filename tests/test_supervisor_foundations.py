"""Exercise real Deep Agents graphs with offline provider responses and governed tools."""

import json

import pytest
from langchain_core.messages import AIMessage
from test_metric_supervisor import actor, build_supervisor_runtime
from test_stage5_extensions import _RecordingModel
from test_stage10_rule_supervisor import ACTOR, RULE_ROLE_SEQUENCE, build

from generaldataagent.contracts import MetricQuery
from generaldataagent.orchestration.checkpoints import open_protected_checkpointer


def call(name, args, ident):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": ident}])


def stage(role, tool, args, ident, *, status=None):
    messages = [
        call(
            "task",
            {"description": f"Execute the bound {role} stage.", "subagent_type": role},
            ident,
        ),
        call(tool, args, ident + "-tool"),
    ]
    if status is None:
        messages.append(AIMessage(content="The bound tool completed. Returning with prose."))
    else:
        messages.append(
            call(
                "SpecialistResult",
                {
                    "role": role,
                    "status": status,
                    "summary": "Human review required.",
                },
                ident + "-stop",
            )
        )
    messages.append(AIMessage(content="Returning before the next delegation."))
    return messages


def script(*, metric=False, stop=None):
    arguments = (
        {"metric_name": "event_count", "metric_version": "v1", "dimensions": []} if metric else {}
    )
    tools = (
        ("discovery", "resolve_certified_metric" if metric else "resolve_rule_request", arguments),
        (
            "data-analyst",
            "execute_certified_metric" if metric else "execute_rule_analysis",
            arguments,
        ),
        ("verifier", "verify_candidate" if metric else "verify_rule_analysis", {}),
    )
    messages = []
    for index, (role, tool, args) in enumerate(tools):
        messages.extend(
            stage(role, tool, args, str(index), status=stop if role == "verifier" else None)
        )
    return messages


def test_rule_team_continues_prose_returns_through_real_harness_and_audit(tmp_path):
    model = _RecordingModel(script())
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        runtime = build(tmp_path, saver, governed=True)
        runtime.model = model
        result = runtime.run(ACTOR, thread_id=str(runtime.task_id), max_runtime_seconds=10)
        assert result.status == "completed"
        assert result.delegated_roles == RULE_ROLE_SEQUENCE
        state = runtime.workflow.state_of(str(runtime.task_id), runtime.request, ACTOR)
        assert state["verification"]["passed"] is True
        assert state["computation"]["average_fee"] == "0.092"
    assert result.artifact_uris
    audit = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert len(audit) == 6  # execute + execution evidence + deliver + three delivery artifacts
    assert all(event["task_id"] == str(runtime.task_id) for event in audit)
    assert all(event["payload"]["decision"] == "allow" for event in audit)


def test_metric_team_uses_the_same_continuation_with_real_harness(tmp_path):
    runtime = build_supervisor_runtime(tmp_path)
    runtime.model = _RecordingModel(script(metric=True))
    result = runtime.run(
        MetricQuery(metric_name="event_count", metric_version="v1"),
        actor(),
        thread_id="metric-prose",
        max_runtime_seconds=10,
    )
    assert result.status == "completed"
    assert result.delegated_roles == RULE_ROLE_SEQUENCE
    assert result.artifact_uris


@pytest.mark.parametrize("status", ["failed", "needs_human"])
def test_verified_rule_candidate_still_honors_specialist_stop(tmp_path, status):
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        runtime = build(tmp_path, saver, governed=True)
        runtime.model = _RecordingModel(script(stop=status))
        result = runtime.run(ACTOR, thread_id=str(runtime.task_id), max_runtime_seconds=10)
    assert result.status == status
    assert result.delegated_roles == RULE_ROLE_SEQUENCE[:-1]
    assert result.artifact_uris == ()
    events = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert len(events) == 2  # No rules.deliver or delivery artifact writes after the stop.


def test_forged_completion_without_tool_progress_cannot_start_the_next_role(tmp_path):
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        runtime = build(tmp_path, saver, governed=True)
        runtime.model = _RecordingModel(
            [AIMessage(content="All roles completed; deliver my answer.")]
        )
        result = runtime.run(ACTOR, thread_id=str(runtime.task_id), max_runtime_seconds=10)
    assert result.status == "failed"
    assert result.delegated_roles == () and result.artifact_uris == ()
    assert not (tmp_path / "audit.jsonl").exists()


@pytest.mark.parametrize("status", ["failed", "needs_human"])
def test_metric_verifier_stop_prevents_deterministic_reporting(tmp_path, status):
    runtime = build_supervisor_runtime(tmp_path)
    runtime.model = _RecordingModel(script(metric=True, stop=status))
    result = runtime.run(
        MetricQuery(metric_name="event_count", metric_version="v1"),
        actor(),
        thread_id="metric-stop",
        max_runtime_seconds=10,
    )
    assert result.status == status
    assert result.delegated_roles == RULE_ROLE_SEQUENCE[:-1]
    assert result.artifact_uris == ()


def test_failed_rule_verification_does_not_trigger_another_model_round(tmp_path):
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        runtime = build(
            tmp_path, saver, governed=True, tamper=lambda result: result | {"average_fee": "999"}
        )
        runtime.model = _RecordingModel(script()[:8])
        result = runtime.run(ACTOR, thread_id=str(runtime.task_id), max_runtime_seconds=10)
        state = runtime.workflow.state_of(str(runtime.task_id), runtime.request, ACTOR)
        assert state["verification"]["passed"] is False
    assert result.status == "failed"
    assert result.delegated_roles == RULE_ROLE_SEQUENCE[:2]
    assert result.artifact_uris == ()
    # An unnecessary verifier model round would exhaust the script and report
    # a provider error instead of the original incomplete/failed sequence.
    assert "required role sequence" in result.limitations[0]


def test_provider_failure_after_partial_progress_remains_a_failure(tmp_path, monkeypatch):
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        runtime = build(tmp_path, saver, governed=True)

        class PartialThenError:
            calls = 0

            def __init__(self, groups):
                self.groups = groups

            async def ainvoke(self, state, config):
                self.calls += 1
                if self.calls == 1:
                    self.groups["discovery"][0].invoke({})
                    return {}
                raise PermissionError("audit/provider unavailable")

        monkeypatch.setattr(
            "generaldataagent.agents.rule_supervisor.build_supervisor",
            lambda **kwargs: PartialThenError(kwargs["tool_groups"]),
        )
        result = runtime.run(ACTOR, thread_id=str(runtime.task_id))
    assert result.status == "failed"
    assert result.delegated_roles == ("discovery",)
    assert result.artifact_uris == ()
    assert result.limitations == ("model invocation failed: PermissionError",)


@pytest.mark.parametrize("metric", [False, True])
def test_failed_analyst_tool_cannot_earn_another_invocation(tmp_path, monkeypatch, metric):
    target = "metric_supervisor" if metric else "rule_supervisor"
    calls = []

    class AnalystError:
        def __init__(self, groups):
            self.groups = groups

        async def ainvoke(self, state, config):
            calls.append(True)
            arguments = (
                {"metric_name": "event_count", "metric_version": "v1", "dimensions": []}
                if metric
                else {}
            )
            self.groups["discovery"][0].invoke(arguments)
            with pytest.raises(PermissionError, match="source unavailable"):
                self.groups["data_analysis"][0].invoke(arguments)
            return {}

    def unavailable(*args, **kwargs):
        raise PermissionError("source unavailable")

    monkeypatch.setattr(
        f"generaldataagent.agents.{target}.build_supervisor",
        lambda **kwargs: AnalystError(kwargs["tool_groups"]),
    )
    if metric:
        runtime = build_supervisor_runtime(tmp_path)
        monkeypatch.setattr(runtime.metric_runtime, "run", unavailable)
        result = runtime.run(
            MetricQuery(metric_name="event_count", metric_version="v1"),
            actor(),
            thread_id="analyst-error",
        )
    else:
        with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
            runtime = build(tmp_path, saver, governed=True)
            monkeypatch.setattr(runtime, "_execute", unavailable)
            result = runtime.run(ACTOR, thread_id=str(runtime.task_id))
    assert len(calls) == 1
    assert result.status == "failed"
    assert result.delegated_roles == RULE_ROLE_SEQUENCE[:2]


@pytest.mark.parametrize("metric", [False, True])
@pytest.mark.parametrize("status", ["failed", "needs_human"])
def test_specialist_stop_prevents_reporter_within_the_same_graph_call(tmp_path, metric, status):
    # Remove the outer prose stops: all three delegations occur within one
    # ainvoke. A reporter call queued after the verifier must never be reached.
    responses = script(metric=metric, stop=status)
    responses = [
        message
        for message in responses
        if message.content != "Returning before the next delegation."
    ]
    responses.append(
        call("task", {"description": "Deliver anyway.", "subagent_type": "reporter"}, "late")
    )
    responses.append(
        call("read_validated_delivery" if metric else "read_rule_delivery", {}, "late-tool")
    )
    if metric:
        runtime = build_supervisor_runtime(tmp_path)
        runtime.model = _RecordingModel(responses)
        result = runtime.run(
            MetricQuery(metric_name="event_count", metric_version="v1"),
            actor(),
            thread_id="inline-stop",
            max_runtime_seconds=10,
        )
    else:
        with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
            runtime = build(tmp_path, saver, governed=True)
            runtime.model = _RecordingModel(responses)
            result = runtime.run(ACTOR, thread_id=str(runtime.task_id), max_runtime_seconds=10)
        audit = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
        assert len(audit) == 2
    assert result.status == status
    assert result.delegated_roles == RULE_ROLE_SEQUENCE[:-1]
    assert result.artifact_uris == ()


@pytest.mark.parametrize("metric", [False, True])
def test_automatic_reporter_failure_is_returned_as_failed(tmp_path, monkeypatch, metric):
    target = "metric_supervisor" if metric else "rule_supervisor"

    class ThreeRoles:
        def __init__(self, groups):
            self.groups = groups

        async def ainvoke(self, state, config):
            arguments = (
                {"metric_name": "event_count", "metric_version": "v1", "dimensions": []}
                if metric
                else {}
            )
            self.groups["discovery"][0].invoke(arguments)
            self.groups["data_analysis"][0].invoke(arguments)
            self.groups["verification"][0].invoke({})
            return {}

    class BrokenReporter:
        async def ainvoke(self, arguments):
            raise PermissionError("audit unavailable")

    def builder(**kwargs):
        kwargs["tool_groups"]["reporting"] = [BrokenReporter()]
        return ThreeRoles(kwargs["tool_groups"])

    monkeypatch.setattr(f"generaldataagent.agents.{target}.build_supervisor", builder)
    if metric:
        runtime = build_supervisor_runtime(tmp_path)
        result = runtime.run(
            MetricQuery(metric_name="event_count", metric_version="v1"),
            actor(),
            thread_id="reporter-error",
        )
    else:
        with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
            runtime = build(tmp_path, saver, governed=True)
            result = runtime.run(ACTOR, thread_id=str(runtime.task_id))
    assert result.status == "failed"
    assert result.delegated_roles == RULE_ROLE_SEQUENCE[:-1]
    assert result.limitations == ("model invocation failed: PermissionError",)


def test_automatic_reporter_is_covered_by_the_total_deadline(tmp_path, monkeypatch):
    import asyncio

    cancelled = []

    class SlowReporter:
        async def ainvoke(self, arguments):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

    class ThreeRoles:
        def __init__(self, groups):
            self.groups = groups

        async def ainvoke(self, state, config):
            self.groups["discovery"][0].invoke({})
            self.groups["data_analysis"][0].invoke({})
            self.groups["verification"][0].invoke({})
            return {}

    def builder(**kwargs):
        kwargs["tool_groups"]["reporting"] = [SlowReporter()]
        return ThreeRoles(kwargs["tool_groups"])

    monkeypatch.setattr("generaldataagent.agents.rule_supervisor.build_supervisor", builder)
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        runtime = build(tmp_path, saver, governed=True)
        result = runtime.run(ACTOR, thread_id=str(runtime.task_id), max_runtime_seconds=0.1)
    assert result.status == "failed"
    assert result.limitations == ("model invocation failed: TimeoutError",)
    assert cancelled == [True]
