"""Deep Agents supervisor factory with explicit specialist tool sets."""

from collections.abc import Mapping, Sequence
from typing import Any

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)

from generaldataagent.agents.specs import SPECIALIST_SPECS, SpecialistSpec

SUPERVISOR_PROMPT = """You coordinate governed data work.

First clarify material metric or scope ambiguity. Create a plan, then delegate complex specialist
work using the task tool. Specialists exchange large results through artifact references; do not ask
them to return raw datasets or verbose tool logs. Never grant permissions, bypass validation, or
perform a production mutation. A result can be delivered only after the verifier returns pass.
"""

_GOVERNED_EXCLUDED_TOOLS = frozenset(
    {"ls", "read_file", "write_file", "edit_file", "glob", "grep", "execute"}
)
_PROFILE_REGISTERED = False


def build_supervisor(
    *,
    model: Any,
    tool_groups: Mapping[str, Sequence[Any]],
    backend: Any | None = None,
    checkpointer: Any | None = None,
    specialist_specs: Sequence[SpecialistSpec] = SPECIALIST_SPECS,
    response_formats: Mapping[str, Any] | None = None,
    response_format: Any | None = None,
    system_prompt: str = SUPERVISOR_PROMPT,
) -> Any:
    """Create a model-provider-neutral Deep Agents supervisor.

    Tool lists are explicit by specialist because Deep Agents replaces rather than merges a
    specialist's tools when the field is supplied.
    """

    _register_governed_profiles()
    subagents = []
    for spec in specialist_specs:
        subagent: dict[str, Any] = {
            "name": spec.name,
            "description": spec.description,
            "system_prompt": spec.system_prompt,
            "tools": list(tool_groups.get(spec.tool_group, ())),
        }
        if response_formats and spec.name in response_formats:
            subagent["response_format"] = response_formats[spec.name]
        subagents.append(subagent)

    kwargs: dict[str, Any] = {
        "model": model,
        "system_prompt": system_prompt,
        "tools": list(tool_groups.get("supervisor", ())),
        "subagents": subagents,
    }
    if backend is not None:
        kwargs["backend"] = backend
    if checkpointer is not None:
        kwargs["checkpointer"] = checkpointer
    if response_format is not None:
        kwargs["response_format"] = response_format
    return create_deep_agent(**kwargs)


def _register_governed_profiles() -> None:
    global _PROFILE_REGISTERED  # noqa: PLW0603
    if _PROFILE_REGISTERED:
        return
    profile = HarnessProfile(
        excluded_tools=_GOVERNED_EXCLUDED_TOOLS,
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
    )
    register_harness_profile("anthropic", profile)
    register_harness_profile("openai", profile)
    _PROFILE_REGISTERED = True
