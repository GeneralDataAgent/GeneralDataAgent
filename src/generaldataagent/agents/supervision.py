"""Delegation invariants shared by every governed Supervisor.

Two rules apply to every specialist team and are enforced here rather than in
each runtime, because a second copy of them is a second place to get them wrong:

* roles must run in the declared order, exactly once each;
* a bound tool takes no arguments, and extra arguments are rejected rather than
  silently ignored (LangChain's empty-schema fast path would skip validation).

These are deterministic controls: no model call can skip or reorder a role.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict


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


__all__ = ["NoToolArguments", "record_role"]
