"""Delegation invariants shared by every governed Supervisor.

Shared controls apply to each specialist team rather than being reimplemented
inside each runtime:

* roles must advance in the declared order; repeating the current role is allowed;
* continuation requires new tool progress and stays within the caller's deadline;
* a bound tool takes no arguments, and extra arguments are rejected rather than
  silently ignored (LangChain's empty-schema fast path would skip validation).

These are deterministic controls: no model call can skip or reorder a role.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from typing import Any, Literal

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, ValidationError

from generaldataagent.agents.completion import await_validated_completion
from generaldataagent.agents.contracts import SpecialistResult


class NoToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


def record_role(events: list[str], role: str, sequence: Sequence[str]) -> None:
    """Append ``role`` if it is the one the fixed sequence expects next.

    Repeating the role that just ran is allowed (the same specialist may be
    asked twice); any other out-of-order call is refused.
    """
    if events and events[-1] == role:
        return
    expected = sequence[len(events)] if len(events) < len(sequence) else None
    if role != expected:
        raise PermissionError(f"role {role!r} cannot run; expected {expected!r}")
    events.append(role)


def role_sequence_stop(result: dict[str, Any]) -> Literal["failed", "needs_human"] | None:
    """Honor explicit stops without interpreting free text as authorization."""
    if result.get("__interrupt__"):
        return "needs_human"
    proposed = result.get("structured_response")
    if isinstance(proposed, BaseModel):
        proposed = proposed.model_dump()
    if isinstance(proposed, dict) and proposed.get("status") in {"failed", "needs_human"}:
        return proposed["status"]
    for message in result.get("messages", ()):
        if not isinstance(message, ToolMessage) or message.name != "task":
            continue
        if message.status == "error":
            return "failed"
        if not isinstance(message.content, str):
            continue
        try:
            specialist = SpecialistResult.model_validate(json.loads(message.content))
        except (ValueError, ValidationError):
            continue
        if specialist.status in {"failed", "needs_human"}:
            return specialist.status
    return None


class TeamStopped(Exception):
    """A specialist explicitly refused or requested human review."""

    def __init__(self, status: Literal["failed", "needs_human"]):
        self.status = status
        super().__init__("specialist_team_stopped")


class TeamStopMiddleware(AgentMiddleware):
    """Stop at the task-tool boundary before another delegation can run."""

    @staticmethod
    def _check(request, response):
        if request.tool_call["name"] != "task":
            return response
        if isinstance(response, Command):
            result = response.update if isinstance(response.update, dict) else {}
        else:
            result = {"messages": [response]}
        stop = role_sequence_stop(result)
        if stop is not None:
            raise TeamStopped(stop)
        return response

    def wrap_tool_call(self, request, handler):
        return self._check(request, handler(request))

    async def awrap_tool_call(self, request, handler):
        return self._check(request, await handler(request))


async def continue_role_sequence(
    supervisor: Any,
    initial_state: dict[str, Any],
    config: dict[str, Any],
    *,
    events: list[str],
    sequence: Sequence[str],
    completed: asyncio.Event,
    can_continue: Callable[[], bool],
) -> dict[str, Any]:
    """Resume a prematurely returned graph only after actual tool progress.

    The same graph/thread retains its messages and control dependencies. Each
    additional invocation must follow a newly completed role; repeated calls to
    the last role do not earn another attempt. The final reporter remains the
    caller's deterministic assembly step. Provider errors and cancellation
    propagate to the caller's existing total deadline and failure handling.
    """
    state = initial_state
    for _ in range(len(sequence)):
        before = tuple(events)
        result = await await_validated_completion(supervisor.ainvoke(state, config), completed)
        if completed.is_set():
            return result
        if (
            role_sequence_stop(result) is not None
            or tuple(events) == before
            or tuple(events) != tuple(sequence[: len(events)])
            or len(events) >= len(sequence) - 1
            or not can_continue()
        ):
            return result
        next_role = sequence[len(events)]
        state = {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"The controller recorded completed tool stages: {', '.join(events)}. "
                        f"Continue with {next_role} for the same bound request. "
                        "Use the existing tool results; do not repeat completed stages. "
                        "If a specialist failed or needs human review, return that status."
                    ),
                }
            ]
        }
    return result


__all__ = [
    "NoToolArguments",
    "TeamStopMiddleware",
    "TeamStopped",
    "continue_role_sequence",
    "record_role",
    "role_sequence_stop",
]
