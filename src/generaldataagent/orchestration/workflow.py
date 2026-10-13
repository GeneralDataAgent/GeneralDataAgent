"""LangGraph skeleton for the governed read-only analysis flow."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from generaldataagent.orchestration.state import PhaseOneState

StateHandler = Callable[[PhaseOneState], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class PhaseOneServices:
    """Deterministic or Agent-backed service ports for each workflow node."""

    discover: StateHandler
    plan: StateHandler
    analyze: StateHandler
    validate: StateHandler
    deliver: StateHandler


def build_phase_one_graph(
    services: PhaseOneServices,
    *,
    checkpointer: Any | None = None,
) -> CompiledStateGraph:
    """Build the read-only analysis graph with a non-bypassable validation gate."""

    graph = StateGraph(PhaseOneState)
    graph.add_node("discover", _with_status(services.discover, "discovering"))
    graph.add_node("plan", _with_status(services.plan, "planning"))
    graph.add_node("analyze", _with_status(services.analyze, "running"))
    graph.add_node("validate", _with_status(services.validate, "validating"))
    graph.add_node("deliver", _with_status(services.deliver, "delivering"))
    graph.add_node("fail", _fail_node)

    graph.add_edge(START, "discover")
    graph.add_edge("discover", "plan")
    graph.add_edge("plan", "analyze")
    graph.add_edge("analyze", "validate")
    graph.add_conditional_edges(
        "validate",
        _validation_route,
        {"deliver": "deliver", "fail": "fail"},
    )
    graph.add_edge("deliver", END)
    graph.add_edge("fail", END)
    return graph.compile(checkpointer=checkpointer)


def _with_status(handler: StateHandler, status: str) -> StateHandler:
    def wrapped(state: PhaseOneState) -> dict[str, Any]:
        update = handler(state)
        if "status" not in update:
            update = {**update, "status": status}
        return update

    return wrapped


def _validation_route(state: PhaseOneState) -> Literal["deliver", "fail"]:
    result = state.get("validation_result", {})
    return "deliver" if result.get("status") == "pass" else "fail"


def _fail_node(state: PhaseOneState) -> dict[str, Any]:
    result = state.get("validation_result", {})
    return {
        "status": "needs_human" if result.get("status") == "needs_human" else "failed",
        "errors": [
            {
                "code": "validation_gate_failed",
                "message": "The candidate result did not pass independent validation.",
            }
        ],
    }
