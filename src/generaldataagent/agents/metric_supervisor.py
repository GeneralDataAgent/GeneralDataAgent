"""Real-model Supervisor over an actor-bound certified metric tool surface."""

import asyncio
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

from deepagents.backends import StateBackend
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver

from generaldataagent.agents.contracts import SpecialistResult, SupervisorResult
from generaldataagent.agents.factory import build_supervisor
from generaldataagent.agents.specs import SPECIALIST_SPECS
from generaldataagent.agents.supervision import (
    NoToolArguments,
    TeamStopMiddleware,
    TeamStopped,
    continue_role_sequence,
    record_role,
    role_sequence_stop,
)
from generaldataagent.business.actions import content_hash
from generaldataagent.catalog import InMemoryMetricCatalog
from generaldataagent.contracts import ActorContext, MetricQuery
from generaldataagent.llm.orchestration import OrchestrationRouter
from generaldataagent.orchestration import MetricAnalysisRuntime

_PHASE_ONE_ROLES = frozenset({"discovery", "data-analyst", "verifier", "reporter"})

METRIC_SUPERVISOR_PROMPT = """You coordinate one governed certified-metric request.
You MUST delegate in this order: discovery, data-analyst, verifier, reporter. Do not calculate or
invent data yourself. Pass the exact request JSON to discovery and data-analyst. Verifier and
reporter are already bound to this request's single candidate, so never invent or rewrite an
internal task id. If any role fails or needs_human, stop and return that status. Your final
structured result must cite only artifact URIs returned by the tools. Never use filesystem or shell
capabilities. A discovery result with certification_status=certified, relation, expression,
time_column, owner, and allowed dimensions is sufficient to proceed. The data-analyst must call the
certified executor and must not write SQL itself. You MUST delegate to reporter after verifier
passes; a completed answer without reporter is invalid.
"""

_REQUIRED_ROLE_SEQUENCE = ("discovery", "data-analyst", "verifier", "reporter")


_NoToolArguments = NoToolArguments


class _BoundRequestTool(StructuredTool):
    def _to_args_and_kwargs(self, tool_input, tool_call_id):
        # LangChain's empty-schema fast path skips validation. Explicitly reject extras.
        _NoToolArguments.model_validate(tool_input)
        return (), {}


class MetricSupervisorRuntime:
    """Bind a Supervisor and its specialists to one authenticated actor."""

    def __init__(
        self,
        *,
        model: Any,
        catalog: InMemoryMetricCatalog,
        metric_runtime: MetricAnalysisRuntime,
        orchestration_router: OrchestrationRouter | None = None,
        bound_tool_arguments: bool = False,
    ) -> None:
        if type(bound_tool_arguments) is not bool:
            raise ValueError("bound_tool_arguments must be a boolean")
        self.bound_tool_arguments = bound_tool_arguments
        self.model = model
        self.catalog = catalog
        self.metric_runtime = metric_runtime
        self.orchestration_router = orchestration_router

    def run(
        self,
        query: MetricQuery,
        actor: ActorContext,
        *,
        thread_id: str,
        max_runtime_seconds: float = 60,
    ) -> SupervisorResult:
        return asyncio.run(
            self.arun(
                query,
                actor,
                thread_id=thread_id,
                max_runtime_seconds=max_runtime_seconds,
            )
        )

    async def arun(
        self,
        query: MetricQuery,
        actor: ActorContext,
        *,
        thread_id: str,
        max_runtime_seconds: float = 60,
    ) -> SupervisorResult:
        if max_runtime_seconds <= 0:
            raise ValueError("max_runtime_seconds must be positive")
        candidates: dict[str, dict[str, Any]] = {}
        role_events: list[str] = []
        completed = asyncio.Event()
        loop = asyncio.get_running_loop()
        tool_groups = self._build_actor_tools(
            actor,
            candidates,
            role_events,
            bound_query=query,
            on_validated_delivery=lambda: loop.call_soon_threadsafe(completed.set),
        )
        specs = tuple(spec for spec in SPECIALIST_SPECS if spec.name in _PHASE_ONE_ROLES)
        middleware = (
            (
                self.orchestration_router.for_task(
                    actor,
                    thread_id=thread_id,
                    source_hash=content_hash(query.model_dump(mode="json")),
                ),
            )
            if self.orchestration_router
            else ()
        )
        supervisor = build_supervisor(
            model=self.orchestration_router.bootstrap_model
            if self.orchestration_router
            else self.model,
            model_middleware=(*middleware, TeamStopMiddleware()),
            tool_groups=tool_groups,
            backend=StateBackend(),
            checkpointer=InMemorySaver(),
            specialist_specs=specs,
            response_formats={spec.name: SpecialistResult.model_json_schema() for spec in specs},
            system_prompt=METRIC_SUPERVISOR_PROMPT
            + (
                " All specialist tools already bind the exact authenticated request; "
                "call them with empty argument objects."
                if self.bound_tool_arguments
                else ""
            ),
        )
        request = {
            "metric_query": query.model_dump(mode="json"),
            "actor_scope": {
                "tenant_id": actor.tenant_id,
                "project_id": actor.project_id,
                "data_domains": list(actor.data_domains),
            },
        }
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 40}
        try:
            async with asyncio.timeout(max_runtime_seconds):
                result = await continue_role_sequence(
                    supervisor,
                    {"messages": [{"role": "user", "content": json.dumps(request)}]},
                    config,
                    events=role_events,
                    sequence=_REQUIRED_ROLE_SEQUENCE,
                    completed=completed,
                    can_continue=lambda: len(role_events) < 2 or _candidate_passed(candidates),
                )
                if (
                    role_sequence_stop(result) is None
                    and tuple(role_events) == _REQUIRED_ROLE_SEQUENCE[:-1]
                    and _candidate_passed(candidates)
                ):
                    await tool_groups["reporting"][0].ainvoke({})
        except TeamStopped as exc:
            return SupervisorResult(
                status=exc.status,
                summary="The specialist team stopped before a validated delivery.",
                task_id=None,
                delegated_roles=tuple(role_events),
                artifact_uris=(),
                limitations=("explicit specialist stop or human review required",),
            )
        except Exception as exc:
            candidate = candidates.get("current", {}).get("analysis")
            artifacts = (
                tuple(item["uri"] for item in candidate["delivery_package"]["evidence"])
                if candidate
                else ()
            )
            return SupervisorResult(
                status="failed",
                summary="The model provider did not complete the governed workflow.",
                task_id=candidate["task_id"] if candidate else None,
                delegated_roles=tuple(role_events),
                artifact_uris=artifacts,
                limitations=(f"model invocation failed: {type(exc).__name__}",),
            )
        stop = role_sequence_stop(result)
        if stop is not None and tuple(role_events) != _REQUIRED_ROLE_SEQUENCE:
            candidate = candidates.get("current", {}).get("analysis")
            return SupervisorResult(
                status=stop,
                summary="The metric team stopped before a validated delivery.",
                task_id=candidate["task_id"] if candidate else None,
                delegated_roles=tuple(role_events),
                artifact_uris=(),
                limitations=("explicit team stop or human review required",),
            )
        candidate = candidates.get("current", {}).get("analysis")
        actual_task_id = candidate["task_id"] if candidate else None
        actual_artifacts = (
            tuple(item["uri"] for item in candidate["delivery_package"]["evidence"])
            if candidate
            else ()
        )
        if tuple(role_events) == _REQUIRED_ROLE_SEQUENCE and _candidate_passed(candidates):
            delivery = candidate["delivery_package"]
            return SupervisorResult(
                status="completed",
                summary=delivery["summary"],
                task_id=actual_task_id,
                delegated_roles=tuple(role_events),
                artifact_uris=actual_artifacts,
                limitations=tuple(delivery["limitations"]),
            )
        structured_response = result.get("structured_response")
        if structured_response is None:
            return SupervisorResult(
                status="failed",
                summary="The Supervisor returned no valid structured result.",
                task_id=actual_task_id,
                delegated_roles=tuple(role_events),
                artifact_uris=actual_artifacts,
                limitations=("missing structured Supervisor response",),
            )
        proposed = SupervisorResult.model_validate(structured_response)
        if tuple(role_events) != _REQUIRED_ROLE_SEQUENCE:
            return SupervisorResult(
                status="failed",
                summary="The Supervisor did not complete the required governed role sequence.",
                task_id=actual_task_id,
                delegated_roles=tuple(role_events),
                artifact_uris=actual_artifacts,
                limitations=(
                    f"required role sequence: {', '.join(_REQUIRED_ROLE_SEQUENCE)}",
                    f"actual role sequence: {', '.join(role_events) or 'none'}",
                ),
            )
        return proposed.model_copy(
            update={
                "task_id": actual_task_id,
                "delegated_roles": tuple(role_events),
                "artifact_uris": actual_artifacts,
            }
        )

    def _build_actor_tools(
        self,
        actor: ActorContext,
        candidates: dict[str, dict[str, Any]],
        role_events: list[str] | None = None,
        *,
        bound_query: MetricQuery | None = None,
        on_validated_delivery: Callable[[], None] | None = None,
    ) -> dict[str, list[StructuredTool]]:
        events = role_events if role_events is not None else []

        def resolve_certified_metric(
            metric_name: str,
            metric_version: str,
            dimensions: list[str],
        ) -> dict[str, Any]:
            record_role(events, "discovery", _REQUIRED_ROLE_SEQUENCE)
            if bound_query is not None and (metric_name, metric_version, tuple(dimensions)) != (
                bound_query.metric_name,
                bound_query.metric_version,
                bound_query.dimensions,
            ):
                raise PermissionError("metric discovery differs from the bound request")
            metric = self.catalog.get(metric_name, metric_version)
            if metric.data_domain not in actor.data_domains:
                raise PermissionError(f"unauthorized data domain: {metric.data_domain}")
            invalid = set(dimensions) - set(metric.allowed_dimensions)
            if invalid:
                raise ValueError(f"uncertified dimensions: {sorted(invalid)}")
            return {
                "qualified_metric": metric.qualified_name(),
                "description": metric.description,
                "certification_status": "certified",
                "data_domain": metric.data_domain,
                "relation": metric.relation,
                "expression": metric.expression,
                "time_column": metric.time_column,
                "allowed_dimensions": list(metric.allowed_dimensions),
                "unit": metric.unit,
                "owner": metric.owner,
                "execution_contract": (
                    "Only execute through execute_certified_metric; do not write SQL."
                ),
            }

        def execute_certified_metric(
            metric_name: str,
            metric_version: str,
            dimensions: list[str],
            start_time: datetime | None = None,
            end_time: datetime | None = None,
        ) -> dict[str, Any]:
            record_role(events, "data-analyst", _REQUIRED_ROLE_SEQUENCE)
            proposed_query = MetricQuery(
                metric_name=metric_name,
                metric_version=metric_version,
                dimensions=tuple(dimensions),
                start_time=start_time,
                end_time=end_time,
            )
            if bound_query is not None and proposed_query != bound_query:
                raise PermissionError("metric execution differs from the bound request")
            if "current" in candidates:
                return candidates["current"]["tool_result"]
            result = self.metric_runtime.run(proposed_query, actor)
            delivery = result["delivery_package"]
            tool_result = {
                "task_id": result["task_id"],
                "status": result["status"],
                "validation_status": result["validation_result"]["status"],
                "metric": delivery["metric"],
                "columns": delivery["columns"],
                "rows": delivery["rows"],
                "artifact_uris": [item["uri"] for item in delivery["evidence"]],
            }
            candidates["current"] = {"analysis": result, "tool_result": tool_result}
            return tool_result

        def verify_candidate() -> dict[str, Any]:
            record_role(events, "verifier", _REQUIRED_ROLE_SEQUENCE)
            candidate = _candidate(candidates)
            validation = candidate["validation_result"]
            evidence = candidate["delivery_package"]["evidence"]
            return {
                "task_id": candidate["task_id"],
                "status": validation["status"],
                "checks": validation["checks"],
                "artifact_uris": [item["uri"] for item in evidence],
            }

        def read_validated_delivery() -> dict[str, Any]:
            record_role(events, "reporter", _REQUIRED_ROLE_SEQUENCE)
            candidate = _candidate(candidates)
            if candidate["validation_result"]["status"] != "pass":
                raise PermissionError("candidate has not passed validation")
            if on_validated_delivery is not None:
                on_validated_delivery()
            return candidate["delivery_package"]

        groups = {
            "supervisor": [],
            "discovery": [
                StructuredTool.from_function(
                    resolve_certified_metric,
                    name="resolve_certified_metric",
                    description=(
                        "Resolve one certified metric within the authenticated actor scope."
                    ),
                )
            ],
            "data_analysis": [
                StructuredTool.from_function(
                    execute_certified_metric,
                    name="execute_certified_metric",
                    description=(
                        "Execute one certified metric through policy, read-only SQL, "
                        "and evidence capture."
                    ),
                )
            ],
            "verification": [
                StructuredTool.from_function(
                    verify_candidate,
                    name="verify_candidate",
                    description=(
                        "Read deterministic validation checks for this request's candidate. "
                        "This tool takes no arguments."
                    ),
                )
            ],
            "reporting": [
                StructuredTool.from_function(
                    read_validated_delivery,
                    name="read_validated_delivery",
                    description=(
                        "Read this request's delivery only after deterministic validation passed. "
                        "This tool takes no arguments."
                    ),
                )
            ],
        }
        if self.bound_tool_arguments:
            if bound_query is None:
                raise ValueError("bound tools require an explicit request")

            def discover_bound_request():
                return resolve_certified_metric(
                    bound_query.metric_name,
                    bound_query.metric_version,
                    list(bound_query.dimensions),
                )

            def execute_bound_request():
                return execute_certified_metric(
                    bound_query.metric_name,
                    bound_query.metric_version,
                    list(bound_query.dimensions),
                    bound_query.start_time,
                    bound_query.end_time,
                )

            groups["discovery"] = [
                _BoundRequestTool.from_function(
                    discover_bound_request,
                    name="resolve_certified_metric",
                    description=(
                        "Resolve the exact bound certified metric request. Takes no arguments."
                    ),
                    args_schema=_NoToolArguments,
                )
            ]
            groups["data_analysis"] = [
                _BoundRequestTool.from_function(
                    execute_bound_request,
                    name="execute_certified_metric",
                    description="Execute the bound request with policy and evidence. No arguments.",
                    args_schema=_NoToolArguments,
                )
            ]
            for group in ("verification", "reporting"):
                previous = groups[group][0]
                groups[group] = [
                    _BoundRequestTool.from_function(
                        previous.func,
                        name=previous.name,
                        description=previous.description,
                        args_schema=_NoToolArguments,
                    )
                ]
        return groups


def _candidate(candidates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    try:
        return candidates["current"]["analysis"]
    except KeyError as exc:
        raise ValueError("no analysis candidate exists for this request") from exc


def _candidate_passed(candidates: dict[str, dict[str, Any]]) -> bool:
    try:
        return candidates["current"]["analysis"]["validation_result"]["status"] == "pass"
    except KeyError:
        return False
