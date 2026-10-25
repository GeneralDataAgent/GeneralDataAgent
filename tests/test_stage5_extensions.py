import asyncio
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from admission_helpers import enroll
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from test_data_engineering import BlockingConnector

from generaldataagent.agents.factory import build_supervisor
from generaldataagent.agents.specs import SPECIALIST_SPECS, SpecialistSpec
from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.business.extensions import ExtensionAdmission, SkillDefinition
from generaldataagent.business.governance import CapabilityRegistry
from generaldataagent.connectors.sqlite import SQLiteConnector
from generaldataagent.contracts import ActorContext
from generaldataagent.data_engineering.runtime import DataEngineeringRuntime

ACTOR = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("commerce",))


class FakeGraph:
    def __init__(self):
        self.calls = 0

    def invoke(self, *args, **kwargs):
        self.calls += 1
        return {"done": True}

    async def ainvoke(self, *args, **kwargs):
        return self.invoke(*args, **kwargs)

    def stream(self, *args, **kwargs):
        yield self.invoke(*args, **kwargs)

    async def astream(self, *args, **kwargs):
        yield await self.ainvoke(*args, **kwargs)


def test_new_agent_factory_enforces_pinned_admission_and_runtime_revocation(tmp_path, monkeypatch):
    graph = FakeGraph()
    monkeypatch.setattr("generaldataagent.agents.factory.create_deep_agent", lambda **kw: graph)
    monkeypatch.setattr("generaldataagent.agents.factory._register_governed_profiles", lambda: None)
    spec = SpecialistSpec("custom-agent", "Fixture", "Use only certified evidence", "custom-tools")
    with pytest.raises(PermissionError, match="admission"):
        build_supervisor(model=None, tool_groups={}, specialist_specs=(spec,))
    registry = CapabilityRegistry(tmp_path / "admission", clock=lambda: datetime.now(UTC))
    admission = ExtensionAdmission(registry)
    enroll(registry, ACTOR, "agent", spec.name, admission.agent_hash(spec))
    guarded = build_supervisor(
        model=None,
        tool_groups={},
        specialist_specs=(spec,),
        admission=admission,
        actor=ACTOR,
        agent_versions={spec.name: "v1"},
    )
    assert guarded.invoke({}) == {"done": True}
    assert asyncio.run(guarded.ainvoke({})) == {"done": True}
    assert list(guarded.stream({})) == [{"done": True}]
    registry.revoke(spec.name, "v1")
    with pytest.raises(PermissionError):
        guarded.invoke({})
    with pytest.raises(PermissionError):
        asyncio.run(guarded.ainvoke({}))
    with pytest.raises(PermissionError):
        list(guarded.stream({}))
    assert graph.calls == 3


def test_reviewed_skill_is_applied_and_cannot_expand_tool_authority(tmp_path, monkeypatch):
    captured = {}

    def build(**kwargs):
        captured.update(kwargs)
        return FakeGraph()

    monkeypatch.setattr("generaldataagent.agents.factory.create_deep_agent", build)
    monkeypatch.setattr("generaldataagent.agents.factory._register_governed_profiles", lambda: None)
    spec = SPECIALIST_SPECS[0]
    skill = SkillDefinition(
        name="certified-discovery",
        version="v1",
        prompt_template="Use $domain definitions.",
        allowed_tools=("catalog",),
        applicability=("certified metric lookup",),
        inputs=("domain",),
        outputs=("metric definition",),
        budget=dict(max_tool_calls=3, max_tokens=1000, max_seconds=60),
        verification=("cite the catalog entry",),
        failure_handling=("report ambiguity instead of guessing",),
        examples=(
            dict(kind="positive", request="revenue", expected="the certified definition"),
            dict(kind="negative", request="guessed metric", expected="explicit ambiguity"),
        ),
    )
    registry = CapabilityRegistry(tmp_path / "admission", clock=lambda: datetime.now(UTC))
    admission = ExtensionAdmission(registry)
    enroll(registry, ACTOR, "skill", skill.name, admission.skill_hash(skill))
    groups = {spec.tool_group: [SimpleNamespace(name="catalog"), SimpleNamespace(name="other")]}
    guarded = build_supervisor(
        model=None,
        tool_groups=groups,
        specialist_specs=(spec,),
        admission=admission,
        actor=ACTOR,
        specialist_skills={spec.name: skill},
        skill_context={"domain": "commerce"},
    )
    agent = captured["subagents"][0]
    assert agent["system_prompt"].endswith("Use commerce definitions.")
    assert [t.name for t in agent["tools"]] == ["catalog"]
    changed = skill.model_copy(update={"allowed_tools": ("deploy",)})
    with pytest.raises(PermissionError):
        build_supervisor(
            model=None,
            tool_groups=groups,
            specialist_specs=(spec,),
            admission=admission,
            actor=ACTOR,
            specialist_skills={spec.name: changed},
            skill_context={"domain": "commerce"},
        )
    registry.revoke(skill.name, "v1")
    with pytest.raises(PermissionError):
        guarded.invoke({})


def test_custom_connector_cannot_bypass_admission(tmp_path):
    connector = BlockingConnector(Event(), Event())
    with pytest.raises(PermissionError, match="admission"):
        DataEngineeringRuntime(
            connectors={"blocking": connector},
            connector_domains={"blocking": frozenset({"commerce"})},
            artifacts=LocalArtifactStore(tmp_path / "artifacts"),
        )


def test_admitted_connector_main_profile_path_and_live_revocation(tmp_path):
    database = tmp_path / "source"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE orders (id INTEGER)")
        db.executemany("INSERT INTO orders VALUES(?)", [(1,), (2,)])
    connector = SQLiteConnector(database, connector_id="warehouse")
    configuration = {"source": "fixture-source-v1"}
    registry = CapabilityRegistry(tmp_path / "admission", clock=lambda: datetime.now(UTC))
    admission = ExtensionAdmission(registry)
    enroll(
        registry,
        ACTOR,
        "connector",
        "warehouse",
        admission.connector_hash(connector, configuration),
    )
    runtime = DataEngineeringRuntime(
        connectors={"warehouse": connector},
        connector_domains={"warehouse": frozenset({"commerce"})},
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
        admission=admission,
        connector_versions={"warehouse": "v1"},
        connector_configurations={"warehouse": configuration},
    )
    result = runtime.profile_relation(
        task_id=uuid4(),
        actor=ACTOR,
        connector_id="warehouse",
        relation="orders",
        trace_id="admitted-test",
    )
    assert result.row_count == 2
    # Access outside a policy gateway request has no authenticated actor context.
    with pytest.raises(PermissionError):
        runtime.connectors["warehouse"].fetch_all("SELECT COUNT(*) FROM orders")
    registry.revoke("warehouse", "v1")
    with pytest.raises(PermissionError):
        runtime.profile_relation(
            task_id=uuid4(),
            actor=ACTOR,
            connector_id="warehouse",
            relation="orders",
            trace_id="revoked-test",
        )


def test_agent_pin_detects_changed_prompt_and_wrong_kind(tmp_path, monkeypatch):
    spec = SpecialistSpec("custom", "Fixture", "Original prompt", "tools")
    registry = CapabilityRegistry(tmp_path / "admission", clock=lambda: datetime.now(UTC))
    admission = ExtensionAdmission(registry)
    enroll(registry, ACTOR, "agent", spec.name, admission.agent_hash(spec))
    with pytest.raises(PermissionError):
        admission.agents((replace(spec, system_prompt="Changed"),), ACTOR, {spec.name: "v1"})
    with pytest.raises(PermissionError):
        admission.require("skill", spec.name, "v1", admission.agent_hash(spec), ACTOR)


def test_governed_profile_denies_every_filesystem_tool_including_delete(monkeypatch):
    """A real-model run showed a specialist calling ``delete`` on a host path.

    The harness profile is the only thing standing between a specialist and a
    filesystem write, so the exclusion set is asserted against the harness's
    full list of filesystem tools rather than against a hand-copied subset.
    """
    from deepagents.middleware.filesystem import _ALL_FS_TOOL_NAMES

    from generaldataagent.agents import factory

    registered = {}
    monkeypatch.setattr(factory, "register_harness_profile", registered.__setitem__)
    monkeypatch.setattr(factory, "_PROFILE_REGISTERED", False)
    factory._register_governed_profiles()

    assert set(registered) == {"anthropic", "openai"}
    for profile in registered.values():
        assert set(_ALL_FS_TOOL_NAMES) <= profile.excluded_tools
        # Summarization would call the provider outside model-call governance.
        assert "SummarizationMiddleware" in profile.excluded_middleware
        assert profile.general_purpose_subagent.enabled is False


class _RecordingModel(ChatOpenAI):
    """OpenAI-provider identity, so the governed profile resolves; no network."""

    def __init__(self, script):
        super().__init__(model="test-model", api_key="probe", base_url="http://localhost/v1")
        object.__setattr__(self, "_script", iter(script))
        object.__setattr__(self, "_bindings", [])

    @property
    def bindings(self):
        return self._bindings

    @property
    def script(self):
        return self._script

    def bind_tools(self, tools, **kwargs):
        self.bindings.append(
            {
                "tools": sorted(getattr(tool, "name", str(tool)) for tool in tools),
                "tool_choice": kwargs.get("tool_choice"),
            }
        )
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=next(self.script))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def test_governed_profile_reaches_the_bound_tools_of_a_real_provider_model():
    """The exclusion must hold for the tools actually sent, not just the registry.

    A generic fake model reports its own provider, so the profile never
    resolves for it and any binding assertion about it proves nothing. This
    uses a model whose provider resolves to ``openai`` and records what the
    harness binds on the supervisor turn and on both specialist turns.
    """
    from deepagents.middleware.filesystem import _ALL_FS_TOOL_NAMES

    from generaldataagent.agents.contracts import SpecialistResult

    specs = tuple(spec for spec in SPECIALIST_SPECS if spec.name == "discovery")
    discovery_tool = StructuredTool.from_function(
        lambda: {"operation": "average_fee"},
        name="resolve_rule_request",
        description="Describe the bound rule request.",
    )
    model = _RecordingModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {"description": "discovery", "subagent_type": "discovery"},
                        "id": "1",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[{"name": "resolve_rule_request", "args": {}, "id": "2"}],
            ),
            AIMessage(content="prose"),
            AIMessage(content="prose"),
        ]
    )
    graph = build_supervisor(
        model=model,
        tool_groups={"supervisor": [], "discovery": [discovery_tool]},
        specialist_specs=specs,
        response_formats={spec.name: SpecialistResult.model_json_schema() for spec in specs},
    )
    asyncio.run(
        graph.ainvoke(
            {"messages": [{"role": "user", "content": "go"}]},
            {"configurable": {"thread_id": "profile-probe"}, "recursion_limit": 40},
        )
    )

    filesystem = set(_ALL_FS_TOOL_NAMES)
    assert model.bindings, "the model was never bound"
    for binding in model.bindings:
        assert not filesystem & set(binding["tools"]), binding
    supervisor_turn, specialist_turn = model.bindings[0], model.bindings[1]
    assert supervisor_turn["tools"] == ["task"]
    assert supervisor_turn["tool_choice"] is None
    assert specialist_turn["tools"] == ["SpecialistResult", "resolve_rule_request"]
    assert specialist_turn["tool_choice"] == "any"
