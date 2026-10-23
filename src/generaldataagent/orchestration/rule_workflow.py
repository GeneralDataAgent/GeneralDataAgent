"""Source-bound rule workflow using existing encrypted, actor-scoped checkpoints.

Service ports are trusted application code. This development orchestration does
not grant database access or production admission; handlers must supply their
own governed execution and evidence storage.
"""

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

from generaldataagent.contracts import ActorContext
from generaldataagent.contracts.rule_tools import RuleAnalysisRequest
from generaldataagent.orchestration.checkpoints import ProtectedSqliteSaver


class RuleWorkflowState(TypedDict, total=False):
    request: dict[str, Any]
    source_hashes: dict[str, str]
    workflow_version: str
    binding_hash: str
    monthly_context: dict[str, Any]
    matched_rules: dict[str, Any]
    computation: dict[str, Any]
    verification: dict[str, Any]
    delivery: dict[str, Any]
    status: str


RuleHandler = Callable[[RuleWorkflowState], dict[str, Any]]


@dataclass(frozen=True)
class RuleWorkflowServices:
    monthly_context: RuleHandler
    match_rules: RuleHandler
    compute: RuleHandler
    verify: RuleHandler
    deliver: RuleHandler


def _binding(request, sources, version):
    return hashlib.sha256(
        json.dumps(
            {"request": request, "source_hashes": sources, "workflow_version": version},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _sources(values: Mapping[str, str]) -> dict[str, str]:
    import re

    if not values or any(
        not isinstance(name, str)
        or not name.strip()
        or not isinstance(digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
        for name, digest in values.items()
    ):
        raise ValueError("rule_workflow_source_hashes_required")
    return dict(values)


class RuleWorkflowRuntime:
    """Fixed rule stages; resume only with identical request, source and code version."""

    def __init__(
        self,
        *,
        services: RuleWorkflowServices,
        checkpointer: ProtectedSqliteSaver,
        source_hashes: Callable[[], Mapping[str, str]],
        workflow_version: str,
    ):
        if not isinstance(checkpointer, ProtectedSqliteSaver):
            raise ValueError("protected_rule_checkpointer_required")
        if not workflow_version.strip():
            raise ValueError("rule_workflow_version_required")
        self.checkpointer = checkpointer
        self.source_hashes = source_hashes
        self.workflow_version = workflow_version
        graph = StateGraph(RuleWorkflowState)
        stages = [
            ("monthly_context", services.monthly_context, "monthly_context"),
            ("match_rules", services.match_rules, "matched_rules"),
            ("compute", services.compute, "computation"),
            ("verify", services.verify, "verification"),
            ("deliver", services.deliver, "delivery"),
        ]
        for name, handler, output in stages:
            graph.add_node(name, self._node(handler, output))
        graph.add_edge(START, "monthly_context")
        graph.add_edge("monthly_context", "match_rules")
        graph.add_edge("match_rules", "compute")
        graph.add_edge("compute", "verify")
        graph.add_conditional_edges("verify", self._route, {"deliver": "deliver", "fail": "fail"})
        graph.add_node("fail", lambda _: {"status": "failed"})
        graph.add_edge("deliver", END)
        graph.add_edge("fail", END)
        self.graph = graph.compile(checkpointer=checkpointer)

    @staticmethod
    def _route(state):
        return "deliver" if state.get("verification", {}).get("passed") is True else "fail"

    def _check(self, state, request):
        sources = _sources(self.source_hashes())
        expected = _binding(request, sources, self.workflow_version)
        if (
            state.get("binding_hash") != expected
            or state.get("request") != request
            or state.get("source_hashes") != sources
            or state.get("workflow_version") != self.workflow_version
        ):
            raise ValueError("rule_workflow_binding_changed")

    def _node(self, handler, output):
        def execute(state):
            self._check(state, state["request"])
            # A handler cannot mutate the immutable checkpoint envelope or previous results.
            snapshot = json.loads(json.dumps(state, allow_nan=False))
            result = handler(snapshot)
            if not isinstance(result, dict):
                raise ValueError("rule_workflow_node_requires_object")
            json.dumps(result, allow_nan=False)
            self._check(state, state["request"])
            if output == "verification" and (
                type(result.get("passed")) is not bool
                or not isinstance(result.get("failures"), list)
                or any(not isinstance(code, str) or not code for code in result["failures"])
                or result["passed"] == bool(result["failures"])
            ):
                raise ValueError("rule_workflow_verification_invalid")
            return {output: result, "status": "completed" if output == "delivery" else output}

        return execute

    def run(
        self,
        request: RuleAnalysisRequest,
        actor: ActorContext,
        *,
        interrupt_after: tuple[str, ...] = (),
        thread_id: str | None = None,
    ) -> tuple[str, dict]:
        sources = _sources(self.source_hashes())
        payload = request.model_dump(mode="json")
        state = dict(
            request=payload,
            source_hashes=sources,
            workflow_version=self.workflow_version,
            binding_hash=_binding(payload, sources, self.workflow_version),
            status="created",
        )
        thread = thread_id or str(uuid4())
        self.checkpointer.register_thread(thread, actor)
        with self.checkpointer.execution(thread, actor), tracing_context(enabled=False):
            result = self.graph.invoke(
                state,
                {"configurable": {"thread_id": thread}},
                interrupt_after=list(interrupt_after) or None,
            )
        return thread, result

    def resume(self, thread: str, request: RuleAnalysisRequest, actor: ActorContext) -> dict:
        with self.checkpointer.execution(thread, actor), tracing_context(enabled=False):
            config = {"configurable": {"thread_id": thread}}
            state = self.graph.get_state(config)
            self._check(state.values, request.model_dump(mode="json"))
            if not state.next:
                return state.values
            return self.graph.invoke(None, config)

    def state_of(self, thread: str, request: RuleAnalysisRequest, actor: ActorContext) -> dict:
        """Read a thread's current state without advancing it.

        Delivery must never finish work as a side effect of packaging it, so it
        uses this accessor instead of ``resume``.
        """
        with self.checkpointer.execution(thread, actor), tracing_context(enabled=False):
            state = self.graph.get_state({"configurable": {"thread_id": thread}})
            if not state.values:
                raise ValueError("rule_workflow_thread_missing")
            self._check(state.values, request.model_dump(mode="json"))
            return dict(state.values)
