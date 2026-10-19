"""Enforce reviewed Agent/Skill/connector versions at real assembly and use boundaries."""

import inspect
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
from string import Template
from typing import TYPE_CHECKING, Any

from pydantic import Field

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


class SkillDefinition(ContractModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    version: str = Field(pattern=r"^v[1-9][0-9]*$")
    prompt_template: str = Field(min_length=1, max_length=8000)
    allowed_tools: tuple[str, ...] = Field(default=(), max_length=30)


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

    def render_skill(self, skill: SkillDefinition, actor: ActorContext, context: Mapping[str, str]):
        reference = ("skill", skill.name, skill.version, self.skill_hash(skill))
        self.require(*reference, actor)
        if any(type(value) is not str for value in context.values()):
            raise ValueError("skill context must contain literal text values")
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
