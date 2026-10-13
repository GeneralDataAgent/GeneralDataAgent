"""Deep Agents supervisor factory with explicit specialist tool sets."""

from collections.abc import Mapping, Sequence
from typing import Any

from deepagents import create_deep_agent

from generaldataagent.agents.specs import SPECIALIST_SPECS

SUPERVISOR_PROMPT = """You coordinate governed data work.

First clarify material metric or scope ambiguity. Create a plan, then delegate complex specialist
work using the task tool. Specialists exchange large results through artifact references; do not ask
them to return raw datasets or verbose tool logs. Never grant permissions, bypass validation, or
perform a production mutation. A result can be delivered only after the verifier returns pass.
"""


def build_supervisor(
    *,
    model: Any,
    tool_groups: Mapping[str, Sequence[Any]],
    backend: Any | None = None,
    checkpointer: Any | None = None,
) -> Any:
    """Create a model-provider-neutral Deep Agents supervisor.

    Tool lists are explicit by specialist because Deep Agents replaces rather than merges a
    specialist's tools when the field is supplied.
    """

    subagents = []
    for spec in SPECIALIST_SPECS:
        subagents.append(
            {
                "name": spec.name,
                "description": spec.description,
                "system_prompt": spec.system_prompt,
                "tools": list(tool_groups.get(spec.tool_group, ())),
            }
        )

    kwargs: dict[str, Any] = {
        "model": model,
        "system_prompt": SUPERVISOR_PROMPT,
        "tools": list(tool_groups.get("supervisor", ())),
        "subagents": subagents,
    }
    if backend is not None:
        kwargs["backend"] = backend
    if checkpointer is not None:
        kwargs["checkpointer"] = checkpointer
    return create_deep_agent(**kwargs)
