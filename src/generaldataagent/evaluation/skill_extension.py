"""Explicit admitted instructions for the development bounded action loop."""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from generaldataagent.business.extensions import ExtensionAdmission, SkillDefinition
from generaldataagent.contracts import ActorContext


def action_capabilities(profile: str) -> frozenset[str]:
    """Canonical dispatch keys, independent of JSON or native function aliases."""
    actions = {"sql", "final_sql", "unsupported"}
    if profile != "sql-precision":
        actions.add("answers")
    semantic = profile.startswith("sql-semantic") or profile in {"sql-science", "sql-rules"}
    if semantic:
        actions.update({"contract", "collect", "crosscheck", "answer_sql", "mixed_sql"})
    if profile in {"sql-analysis", "sql-repair", "sql-science", "sql-rules"} or profile in {
        "sql-semantic",
        "sql-semantic-plan",
        "sql-semantic-json",
    }:
        actions.update({"search", "analysis"})
    if profile == "sql-rules":
        actions.add("rules")
    if profile == "sql-semantic-plan":
        actions.add("query_plan")
    return frozenset(actions)


class SkillUseRefused(PermissionError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class BoundSkillProcedure:
    admission: ExtensionAdmission
    actor: ActorContext
    text: str
    references: tuple[tuple[str, str, str, str], ...]
    allowed_actions: frozenset[str]

    def require(self) -> None:
        try:
            for reference in self.references:
                self.admission.require(*reference, self.actor)
        except Exception as exc:
            raise SkillUseRefused("skill_admission_refused") from exc

    def require_action(self, action: dict, available: frozenset[str]) -> None:
        self.require()
        kinds = available.intersection(action)
        if len(kinds) != 1 or not kinds <= self.allowed_actions | {"unsupported"}:
            raise SkillUseRefused("skill_action_not_allowed")

    def summary(self, *, model_responses: int) -> dict:
        return {
            "mode": "admitted_instructions",
            "references": [list(ref) for ref in self.references],
            "allowed_actions": sorted(self.allowed_actions),
            "rendered_hash": "sha256:" + hashlib.sha256(self.text.encode()).hexdigest(),
            "model_responses": model_responses,
            "budget_scope": "declaration_compatibility; existing task meters",
        }


@dataclass(frozen=True)
class BoundedAgentSkill:
    """A reviewed root and explicit prerequisite cache; never enrolls definitions."""

    admission: ExtensionAdmission
    actor: ActorContext
    skill: SkillDefinition
    dependencies: Mapping[str, SkillDefinition] = field(default_factory=dict)

    def __post_init__(self):
        if (
            not isinstance(self.admission, ExtensionAdmission)
            or not isinstance(self.actor, ActorContext)
            or not isinstance(self.skill, SkillDefinition)
            or not isinstance(self.dependencies, Mapping)
        ):
            raise ValueError("bounded skill requires explicit admission, actor and definitions")
        object.__setattr__(self, "dependencies", MappingProxyType(dict(self.dependencies)))

    def bind(
        self, context: Mapping[str, str], *, available: frozenset[str], task_budget: Any
    ) -> BoundSkillProcedure:
        if not set(self.skill.allowed_tools) <= available:
            raise PermissionError("skill_actions_absent_from_profile")
        text, references = self.admission.render_skill_bundle(
            self.skill,
            self.actor,
            context,
            dependencies=self.dependencies,
            task_budget=task_budget,
        )
        return BoundSkillProcedure(
            self.admission, self.actor, text, references, frozenset(self.skill.allowed_tools)
        )
