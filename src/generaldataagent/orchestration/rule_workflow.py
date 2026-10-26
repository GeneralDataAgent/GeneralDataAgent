"""Source-bound rule workflow using existing encrypted, actor-scoped checkpoints.

Service ports are trusted application code. This development orchestration does
not grant database access or production admission; handlers must supply their
own governed execution and evidence storage.
"""

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypedDict
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
    verification_scope: str
    delivery: dict[str, Any]
    status: str
    context_input_hash: str
    context_sources: list[str]
    parent_thread_id: str
    reused_stages: list[str]


RuleHandler = Callable[[RuleWorkflowState], dict[str, Any]]


@dataclass(frozen=True)
class RuleWorkflowServices:
    monthly_context: RuleHandler
    match_rules: RuleHandler
    compute: RuleHandler
    verify: RuleHandler
    deliver: RuleHandler
    # Trusted application wiring declares the implementation's checking strength.
    verification_scope: Literal["producer_self_check", "authoritative_rederivation"] = (
        "producer_self_check"
    )
    # None means unknown dependencies: never reuse the context across tasks.
    context_sources: tuple[str, ...] | None = None


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
        if services.verification_scope not in {"producer_self_check", "authoritative_rederivation"}:
            raise ValueError("rule_workflow_verification_scope_invalid")
        self.verification_scope = services.verification_scope
        self.context_sources = services.context_sources
        if self.context_sources is not None and (
            len(set(self.context_sources)) != len(self.context_sources)
            or any(not isinstance(name, str) or not name for name in self.context_sources)
        ):
            raise ValueError("rule_workflow_context_dependencies_invalid")
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
        graph.add_conditional_edges(
            START,
            lambda state: "match_rules" if "monthly_context" in state else "monthly_context",
            {"match_rules": "match_rules", "monthly_context": "monthly_context"},
        )
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
            update = {output: result, "status": "completed" if output == "delivery" else output}
            if (
                output == "monthly_context"
                and self.context_sources is not None
                and set(self.context_sources) <= set(state["source_hashes"])
            ):
                update["context_sources"] = list(self.context_sources)
                update["context_input_hash"] = self._context_hash(state)
            if output == "verification":
                # This checkpoint channel is owned by the wrapper. Legacy handler
                # dictionaries may contain arbitrary scope metadata and are untrusted.
                update["verification_scope"] = self.verification_scope
            return update

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

    def _context_hash(self, state):
        if self.context_sources is None or not set(self.context_sources) <= set(
            state["source_hashes"]
        ):
            raise ValueError("rule_workflow_context_dependencies_missing")
        return _binding(
            state["request"],
            {name: state["source_hashes"][name] for name in self.context_sources},
            state["workflow_version"],
        )

    def recompute(
        self,
        parent_thread: str,
        request: RuleAnalysisRequest,
        actor: ActorContext,
        *,
        thread_id: str | None = None,
    ) -> tuple[str, dict]:
        """Fork a completed claim under new sources; never rewrite its old evidence.

        Only context with an identical declared dependency fingerprint is reused.
        Unknown/legacy dependencies recompute all stages. Matching and all later
        stages always rerun, including authoritative verification.
        """
        original_actor = self.checkpointer.authorize_thread(parent_thread, actor)
        if (actor.user_id, actor.tenant_id, actor.project_id) != (
            original_actor.user_id,
            original_actor.tenant_id,
            original_actor.project_id,
        ) or set(actor.data_domains) != set(original_actor.data_domains):
            raise PermissionError("rule_workflow_recompute_actor_changed")
        with self.checkpointer.execution(parent_thread, actor), tracing_context(enabled=False):
            parent = dict(
                self.graph.get_state({"configurable": {"thread_id": parent_thread}}).values
            )
        payload = request.model_dump(mode="json")
        if (
            parent.get("request") != payload
            or parent.get("workflow_version") != self.workflow_version
            or parent.get("status") not in {"completed", "failed"}
            or parent.get("binding_hash")
            != _binding(payload, _sources(parent.get("source_hashes", {})), self.workflow_version)
        ):
            raise ValueError("rule_workflow_recompute_parent_invalid")
        sources = _sources(self.source_hashes())
        state = dict(
            request=payload,
            source_hashes=sources,
            workflow_version=self.workflow_version,
            binding_hash=_binding(payload, sources, self.workflow_version),
            status="created",
            parent_thread_id=parent_thread,
            reused_stages=[],
        )
        if (
            self.context_sources is not None
            and set(self.context_sources) <= set(sources)
            and set(self.context_sources) <= set(parent["source_hashes"])
            and parent.get("context_sources") == list(self.context_sources)
            and parent.get("context_input_hash") == self._context_hash(state)
            and parent.get("context_input_hash") == self._context_hash(parent)
            and isinstance(parent.get("monthly_context"), dict)
        ):
            state.update(
                monthly_context=json.loads(json.dumps(parent["monthly_context"], allow_nan=False)),
                context_sources=list(self.context_sources),
                context_input_hash=parent["context_input_hash"],
                reused_stages=["monthly_context"],
            )
        thread = thread_id or str(uuid4())
        self.checkpointer.register_thread(thread, actor)
        with self.checkpointer.execution(thread, actor), tracing_context(enabled=False):
            result = self.graph.invoke(state, {"configurable": {"thread_id": thread}})
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
