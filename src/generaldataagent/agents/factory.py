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
from generaldataagent.business.extensions import AdmittedGraph, ExtensionAdmission, SkillDefinition
from generaldataagent.contracts import ActorContext

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
    admission: ExtensionAdmission | None = None,
    actor: ActorContext | None = None,
    agent_versions: Mapping[str, str] | None = None,
    specialist_skills: Mapping[str, SkillDefinition] | None = None,
    skill_context: Mapping[str, str] | None = None,
    model_middleware: Sequence[Any] = (),
) -> Any:
    """Create a model-provider-neutral Deep Agents supervisor.

    Tool lists are explicit by specialist because Deep Agents replaces rather than merges a
    specialist's tools when the field is supplied.
    """

    if len({s.name for s in specialist_specs}) != len(specialist_specs):
        raise ValueError("specialist names must be unique")
    if admission is None and (
        any(s not in SPECIALIST_SPECS for s in specialist_specs)
        or specialist_skills
        or agent_versions
    ):
        raise PermissionError("custom agents and skills require explicit admission")
    if specialist_skills and (
        actor is None or not set(specialist_skills) <= {s.name for s in specialist_specs}
    ):
        raise ValueError("skill assignments require an actor and a selected specialist")
    references = (
        admission.agents(specialist_specs, actor, agent_versions or {}) if admission else []
    )
    _register_governed_profiles()
    subagents = []
    for spec in specialist_specs:
        subagent: dict[str, Any] = {
            "name": spec.name,
            "description": spec.description,
            "system_prompt": spec.system_prompt,
            "tools": list(tool_groups.get(spec.tool_group, ())),
        }
        if specialist_skills and spec.name in specialist_skills:
            skill = specialist_skills[spec.name]
            rendered, reference = admission.render_skill(skill, actor, skill_context or {})
            references.append(reference)
            tools_by_name = {getattr(tool, "name", ""): tool for tool in subagent["tools"]}
            if not set(skill.allowed_tools) <= tools_by_name.keys():
                raise PermissionError("skill cannot grant tools absent from its specialist")
            subagent["tools"] = [tools_by_name[name] for name in skill.allowed_tools]
            subagent["system_prompt"] += "\n\n" + rendered
        if response_formats and spec.name in response_formats:
            subagent["response_format"] = response_formats[spec.name]
        if model_middleware:
            subagent["middleware"] = list(model_middleware)
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
    if model_middleware:
        kwargs["middleware"] = list(model_middleware)
    graph = create_deep_agent(**kwargs)
    return AdmittedGraph(graph, admission, actor, references) if references else graph


def _register_governed_profiles() -> None:
    global _PROFILE_REGISTERED  # noqa: PLW0603
    if _PROFILE_REGISTERED:
        return
    profile = HarnessProfile(
        excluded_tools=_GOVERNED_EXCLUDED_TOOLS,
        # Automatic summarization calls the provider outside model-call governance.
        excluded_middleware=frozenset({"SummarizationMiddleware"}),
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
    )
    register_harness_profile("anthropic", profile)
    register_harness_profile("openai", profile)
    _PROFILE_REGISTERED = True
