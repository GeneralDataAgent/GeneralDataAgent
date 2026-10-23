"""Enforce reviewed Agent/Skill/connector versions at real assembly and use boundaries."""

import inspect
import re
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
from string import Template
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, field_validator, model_validator

from generaldataagent.business.actions import content_hash
from generaldataagent.business.governance import CapabilityRegistry
from generaldataagent.contracts import ActorContext, ToolRequest
from generaldataagent.contracts.models import ContractModel
from generaldataagent.security.dlp import TextDLPScanner

if TYPE_CHECKING:
    from generaldataagent.agents.specs import SpecialistSpec

_CURRENT_ACTOR: ContextVar[ActorContext | None] = ContextVar(
    "admitted_connector_actor", default=None
)


class SkillExample(ContractModel):
    """One synthetic example proving the skill's declared behaviour.

    A skill without a negative example has not shown what it must refuse, so
    both kinds are required rather than optional documentation.
    """

    kind: Literal["positive", "negative"]
    request: str = Field(min_length=1, max_length=2000)
    expected: str = Field(min_length=1, max_length=2000)


class SkillBudget(ContractModel):
    """Upper bounds a skill may consume; checked against the task budget."""

    max_tool_calls: int = Field(ge=1, le=1000)
    max_tokens: int = Field(ge=1)
    max_seconds: int = Field(ge=1, le=86_400)


class SkillDefinition(ContractModel):
    """A versioned, reviewed, executable skill with an explicit contract.

    Every field is part of the admission hash. Nothing here grants a tool
    permission by itself: ``allowed_tools`` can only narrow the specialist's
    existing tools, and data domains can only narrow the actor's scope.
    """

    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    version: str = Field(pattern=r"^v[1-9][0-9]*$")
    prompt_template: str = Field(min_length=1, max_length=8000)
    allowed_tools: tuple[str, ...] = Field(min_length=1, max_length=30)
    applicability: tuple[str, ...] = Field(min_length=1, max_length=20)
    inputs: tuple[str, ...] = Field(min_length=1, max_length=30)
    outputs: tuple[str, ...] = Field(min_length=1, max_length=30)
    required_data_domains: tuple[str, ...] = Field(default=(), max_length=32)
    budget: SkillBudget
    verification: tuple[str, ...] = Field(min_length=1, max_length=30)
    failure_handling: tuple[str, ...] = Field(min_length=1, max_length=30)
    dependencies: tuple[str, ...] = Field(default=(), max_length=20)
    examples: tuple[SkillExample, ...] = Field(min_length=2, max_length=50)

    @field_validator("allowed_tools", "applicability", "inputs", "outputs")
    @classmethod
    def _non_blank(cls, values):
        if any(not value.strip() for value in values):
            raise ValueError("skill declarations must not be blank")
        if len(set(values)) != len(values):
            raise ValueError("skill declarations must be unique")
        return values

    @field_validator("dependencies")
    @classmethod
    def _dependencies_named(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("skill dependencies must be unique")
        pattern = r"[a-z][a-z0-9_.-]{0,127}@v[1-9][0-9]*"
        if any(re.fullmatch(pattern, value) is None for value in values):
            raise ValueError("skill dependencies must be name@version")
        return values

    @model_validator(mode="after")
    def _coherent(self):
        kinds = {example.kind for example in self.examples}
        if kinds != {"positive", "negative"}:
            raise ValueError("skill requires at least one positive and one negative example")
        if any(value.split("@")[0] == self.name for value in self.dependencies):
            raise ValueError("skill cannot depend on itself")
        if not set(self.failure_handling):
            raise ValueError("skill must declare failure handling")
        return self


class ExtensionAdmission:
    def __init__(self, registry: CapabilityRegistry):
        self.registry = registry

    @staticmethod
    def agent_hash(spec: "SpecialistSpec") -> str:
        return content_hash({"kind": "agent", "spec": asdict(spec)})

    @staticmethod
    def skill_hash(skill: SkillDefinition) -> str:
        return content_hash({"kind": "skill", "spec": skill.model_dump(mode="json")})

    @staticmethod
    def connector_hash(connector, configuration: dict) -> str:
        TextDLPScanner().check(str(configuration).encode())
        return content_hash(
            {
                "kind": "connector",
                "implementation": inspect.getsource(type(connector)),
                "type": f"{type(connector).__module__}.{type(connector).__qualname__}",
                "methods": {
                    name: inspect.getsource(getattr(connector, name))
                    for name in ("inspect_relation", "fetch_all")
                },
                "database_path": str(getattr(connector, "database_path", "")),
                "timeout_seconds": getattr(connector, "timeout_seconds", None),
                "capabilities": connector.capabilities.model_dump(mode="json"),
                "configuration": configuration,
            }
        )

    def require(self, kind, name, version, digest, actor):
        record = self.registry.require(name, version, digest, actor)
        if record.kind != kind:
            raise PermissionError("extension_kind_mismatch")

    def agents(self, specs, actor, versions):
        from generaldataagent.agents.specs import SPECIALIST_SPECS

        references = []
        for spec in specs:
            if spec not in SPECIALIST_SPECS or spec.name in versions:
                if actor is None or spec.name not in versions:
                    raise PermissionError("custom_agent_requires_pinned_admission")
                ref = ("agent", spec.name, versions[spec.name], self.agent_hash(spec))
                self.require(*ref, actor)
                references.append(ref)
        return references

    def render_skill(
        self,
        skill: SkillDefinition,
        actor: ActorContext,
        context: Mapping[str, str],
        *,
        task_budget: Any | None = None,
    ):
        reference = ("skill", skill.name, skill.version, self.skill_hash(skill))
        self.require(*reference, actor)
        if any(type(value) is not str for value in context.values()):
            raise ValueError("skill context must contain literal text values")
        missing = set(skill.inputs) - set(context)
        if missing:
            raise ValueError("skill_context_missing_declared_inputs")
        # A skill may narrow the actor's data scope but never widen it.
        if not set(skill.required_data_domains) <= set(actor.data_domains):
            raise PermissionError("skill_data_domain_not_granted")
        if task_budget is not None:
            declared = (
                ("max_tool_calls", skill.budget.max_tool_calls),
                ("max_tokens", skill.budget.max_tokens),
                ("max_runtime_seconds", skill.budget.max_seconds),
            )
            for name, value in declared:
                allowed = getattr(task_budget, name, None)
                if allowed is not None and value > allowed:
                    raise PermissionError("skill_budget_exceeds_task_budget")
        text = Template(skill.prompt_template).substitute(context)
        if len(text) > 16000:
            raise ValueError("rendered skill is too large")
        TextDLPScanner().check(text.encode())
        return text, reference

    @contextmanager
    def tool_scope(self, request: ToolRequest):
        actor = ActorContext(
            user_id=request.user_id,
            tenant_id=request.tenant_id,
            project_id=request.project_id,
            data_domains=request.data_domains,
        )
        token = _CURRENT_ACTOR.set(actor)
        try:
            yield
        finally:
            _CURRENT_ACTOR.reset(token)

    def connector(self, connector, *, name: str, version: str, configuration: dict):
        return AdmittedConnector(self, connector, name, version, configuration)


class AdmittedConnector:
    def __init__(self, admission, connector, name, version, configuration):
        self.admission, self._connector = admission, connector
        self.name, self.version = name, version
        self.configuration = dict(configuration)
        self.digest = admission.connector_hash(connector, self.configuration)
        caps = connector.capabilities
        if not caps.read_only or caps.dml or caps.ddl:
            raise PermissionError("extension_connector_must_be_read_only")

    @property
    def capabilities(self):
        return self._connector.capabilities

    @property
    def database_path(self):
        return getattr(self._connector, "database_path", None)

    def _check(self):
        actor = _CURRENT_ACTOR.get()
        if (
            actor is None
            or self.admission.connector_hash(self._connector, self.configuration) != self.digest
        ):
            raise PermissionError("connector_requires_current_actor_and_pinned_content")
        self.admission.require("connector", self.name, self.version, self.digest, actor)

    def inspect_relation(self, relation, *, cancel_event=None):
        self._check()
        result = self._connector.inspect_relation(relation, cancel_event=cancel_event)
        self._check()
        return result

    def fetch_all(self, sql, parameters=(), *, cancel_event=None):
        self._check()
        result = self._connector.fetch_all(sql, parameters, cancel_event=cancel_event)
        self._check()
        return result


class AdmittedGraph:
    def __init__(self, graph: Any, admission: ExtensionAdmission, actor: ActorContext, references):
        self._graph, self.admission, self.actor, self.references = (
            graph,
            admission,
            actor,
            tuple(references),
        )

    def _check(self):
        for reference in self.references:
            self.admission.require(*reference, self.actor)

    def invoke(self, *args, **kwargs):
        self._check()
        result = self._graph.invoke(*args, **kwargs)
        self._check()
        return result

    async def ainvoke(self, *args, **kwargs):
        self._check()
        result = await self._graph.ainvoke(*args, **kwargs)
        self._check()
        return result

    def stream(self, *args, **kwargs):
        self._check()
        for chunk in self._graph.stream(*args, **kwargs):
            self._check()
            yield chunk
        self._check()

    async def astream(self, *args, **kwargs):
        self._check()
        async for chunk in self._graph.astream(*args, **kwargs):
            self._check()
            yield chunk
        self._check()
