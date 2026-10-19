import asyncio
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from admission_helpers import enroll
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
