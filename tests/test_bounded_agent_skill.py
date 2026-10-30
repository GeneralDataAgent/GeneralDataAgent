"""Admitted procedures narrow the actual bounded agent across protocol forms."""

import json
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from admission_helpers import enroll
from test_stage6_evaluation import Model, case

from generaldataagent.business.extensions import ExtensionAdmission, SkillDefinition
from generaldataagent.business.governance import CapabilityRegistry
from generaldataagent.contracts import ActorContext
from generaldataagent.evaluation.skill_extension import BoundedAgentSkill
from generaldataagent.evaluation.stage_six import BoundedSQLAgent, Task

ACTOR = ActorContext(user_id="fixture", tenant_id="t", project_id="p", data_domains=("commerce",))


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "data.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE data(value REAL)")
        connection.executemany("INSERT INTO data VALUES(?)", [(1,), (2,), (3,)])
    return path


def extension(tmp_path, *, actions=("sql", "answers"), dependencies=False, **changes):
    admission = ExtensionAdmission(
        CapabilityRegistry(tmp_path / "admission.sqlite", clock=lambda: datetime.now(UTC))
    )
    values = dict(
        name="bounded-procedure",
        version="v1",
        prompt_template=(
            "Reviewed procedure for $question: use observed evidence; $document_outline"
        ),
        allowed_tools=actions,
        applicability=("synthetic bounded analysis",),
        inputs=("question", "document_outline"),
        outputs=("evidence-bound answer",),
        required_data_domains=("commerce",),
        budget=dict(max_tool_calls=6, max_tokens=1000, max_seconds=30),
        verification=("retain exact evidence steps",),
        failure_handling=("report unsupported capability",),
        examples=(
            dict(kind="positive", request="mean", expected="compute from data"),
            dict(kind="negative", request="guess", expected="refuse unsupported evidence"),
        ),
    )
    definitions = {}
    if dependencies:
        leaf = SkillDefinition(**(values | {"name": "citation-policy"}))
        definitions["citation-policy@v1"] = leaf
        values["dependencies"] = ("citation-policy@v1",)
    root = SkillDefinition(**(values | changes))
    for item in (root, *definitions.values()):
        enroll(admission.registry, ACTOR, "skill", item.name, admission.skill_hash(item))
    return BoundedAgentSkill(admission, ACTOR, root, definitions)


def model_actions():
    return (
        {"sql": "SELECT AVG(value) AS mean FROM data"},
        {"answers": {"mean": "2"}, "evidence_steps": [1]},
    )


def test_real_sql_and_admitted_dependency_procedure_reach_the_agent(database, tmp_path):
    value = extension(tmp_path, dependencies=True)
    supplied = dict(value.dependencies)
    snapshot = BoundedAgentSkill(value.admission, ACTOR, value.skill, supplied)
    supplied.clear()
    model = Model(*model_actions())
    result = BoundedSQLAgent(model, skill_extension=snapshot).run(
        case("SECRET_REFERENCE_VALUE").task, database
    )
    assert result["failure"] is None and result["answers"] == {"mean": "2"}
    assert [ref[1] for ref in result["skill_extension"]["references"]] == [
        "citation-policy",
        "bounded-procedure",
    ]
    assert result["skill_extension"]["model_responses"] == 2
    assert "Reviewed procedure for Mean?" in model.messages[0][1]
    assert "SECRET_REFERENCE_VALUE" not in str(model.messages)
    with pytest.raises(TypeError):
        snapshot.dependencies["other@v1"] = value.skill


@pytest.mark.parametrize(
    "profile",
    [
        "sql",
        "sql-analysis",
        "sql-repair",
        "sql-precision",
        "sql-semantic",
        "sql-semantic-plan",
        "sql-semantic-json",
        "sql-science",
        "sql-rules",
    ],
)
def test_profile_prompt_replacement_preserves_instructions(database, tmp_path, profile):
    model = Model({"unsupported": "synthetic exit"})
    sql_output = profile in {"sql-precision", "sql-semantic-plan", "sql-semantic-json"}
    task = (
        Task("bird-mini-dev-sqlite", "fixture", "Mean?", "", "easy", "fixture")
        if sql_output
        else case().task
    )
    actions = ("sql", "final_sql") if sql_output else ("sql", "answers")
    result = BoundedSQLAgent(
        model, tool_profile=profile, skill_extension=extension(tmp_path, actions=actions)
    ).run(task, database)
    assert result["failure"] == "unsupported_capability"
    assert "Reviewed procedure" in model.messages[0][1]


@pytest.mark.parametrize(
    "change", ["revoked", "hash", "actor", "missing", "tools", "budget", "inputs"]
)
def test_invalid_procedure_fails_before_database_access_or_model(tmp_path, monkeypatch, change):
    value = extension(tmp_path, dependencies=True)
    if change == "revoked":
        value.admission.registry.revoke("citation-policy", "v1")
    elif change == "hash":
        value = BoundedAgentSkill(
            value.admission,
            ACTOR,
            value.skill.model_copy(update={"prompt_template": "Changed"}),
            value.dependencies,
        )
    elif change == "actor":
        value = BoundedAgentSkill(
            value.admission,
            ACTOR.model_copy(update={"tenant_id": "foreign"}),
            value.skill,
            value.dependencies,
        )
    elif change == "missing":
        value = BoundedAgentSkill(value.admission, ACTOR, value.skill)
    elif change == "tools":
        value = extension(tmp_path / "tools", actions=("search",))
    elif change == "budget":
        value = extension(
            tmp_path / "budget", budget=dict(max_tool_calls=7, max_tokens=1000, max_seconds=30)
        )
    elif change == "inputs":
        value = extension(
            tmp_path / "inputs", inputs=("private_answer",), prompt_template="$private_answer"
        )
    reads = []
    monkeypatch.setattr(
        "generaldataagent.evaluation.stage_six.database_context", lambda *a, **kw: reads.append(a)
    )
    model = Model()
    with pytest.raises((ValueError, PermissionError)):
        BoundedSQLAgent(model, skill_extension=value).run(case().task, tmp_path / "absent.sqlite")
    assert model.messages == [] and reads == []


def test_unauthorized_json_action_is_terminal_without_tool_or_repair(
    database, tmp_path, monkeypatch
):
    value = extension(tmp_path)
    reads = []
    monkeypatch.setattr(
        "generaldataagent.evaluation.stage_six_analysis.retrieve_documents",
        lambda *a: reads.append(a),
    )
    result = BoundedSQLAgent(
        Model({"search": "scope"}), tool_profile="sql-repair", skill_extension=value
    ).run(case().task, database, documents="# Scope\nScope is local")
    assert result["failure"] == "skill_action_not_allowed" and result["answers"] == {}
    assert result["repair_budget"]["attempted"] == 0 and reads == []
    assert result["tokens"] == 10


def test_explicit_document_search_and_sql_share_the_admitted_action_surface(database, tmp_path):
    value = extension(tmp_path, actions=("search", "sql", "answers"))
    model = Model(
        {"search": "mean"},
        {"sql": "SELECT AVG(value) AS mean FROM data"},
        {"answers": {"mean": "2"}, "evidence_steps": [2]},
    )
    result = BoundedSQLAgent(model, tool_profile="sql-analysis", skill_extension=value).run(
        case().task, database, documents="# Measure\nMean uses the complete input population."
    )
    assert result["failure"] is None and result["answers"] == {"mean": "2"}
    assert [row["tool"] for row in result["trace"]] == ["document_search", "sql"]
    assert result["skill_extension"]["model_responses"] == 3
    assert "# Measure" in model.messages[0][1]
    assert "untrusted_excerpt" in str(model.messages)


class NativeModel(Model):
    def __init__(self, *actions):
        super().__init__(*actions)
        self.bindings = []

    def bind(self, **kwargs):
        self.bindings.append(kwargs)
        return self

    def invoke(self, messages):
        response = super().invoke(messages)
        call = json.loads(response.content)
        return SimpleNamespace(
            content="", usage_metadata=response.usage_metadata, tool_calls=[call]
        )


def native(name, args):
    return {"id": "fixture", "name": name, "args": args, "type": "tool_call"}


def test_native_surface_and_execution_are_narrowed_together(database, tmp_path):
    model = NativeModel(
        native("compute_sql", {"sql": "SELECT AVG(value) AS mean FROM data"}),
        native("submit_answers", {"fields": [{"name": "mean", "step": 1, "path": ["rows", 0, 0]}]}),
    )
    result = BoundedSQLAgent(
        model, tool_profile="sql-semantic", skill_extension=extension(tmp_path)
    ).run(case().task, database)
    assert result["failure"] is None and result["answers"] == {"mean": 2.0}
    for binding in model.bindings:
        names = {tool["function"]["name"] for tool in binding["tools"]}
        assert names <= {"compute_sql", "execute_sql", "submit_answers", "declare_unsupported"}
        assert "declare_unsupported" in names
        assert binding["parallel_tool_calls"] is False


def test_unadvertised_native_alias_cannot_bypass_action_gate(database, tmp_path):
    model = NativeModel(native("submit_sql_answers", {"sql": "SELECT 99 AS mean FROM data"}))
    result = BoundedSQLAgent(
        model, tool_profile="sql-semantic", skill_extension=extension(tmp_path)
    ).run(case().task, database)
    assert result["failure"] == "skill_action_not_allowed"
    assert not any(row.get("status") == "ok" for row in result["trace"])
    assert result["answers"] == {} and result["final_sql"] is None


def test_revocation_during_model_keeps_usage_and_executes_no_action(database, tmp_path):
    value = extension(tmp_path, dependencies=True)

    class RevokeModel(Model):
        def invoke(self, messages):
            response = super().invoke(messages)
            value.admission.registry.revoke("citation-policy", "v1")
            response.response_metadata = {"token_usage": {"cost": 0.001}}
            return response

    result = BoundedSQLAgent(RevokeModel(*model_actions()), skill_extension=value).run(
        case().task, database
    )
    assert result["failure"] == "skill_admission_refused"
    assert result["tokens"] == 10 and result["cost_usd"] == 0.001
    assert result["skill_extension"]["model_responses"] == 1
    assert not any(row.get("status") == "ok" for row in result["trace"])


@pytest.mark.parametrize("phase", ["sql", "submission"])
def test_revocation_during_tool_or_submission_prevents_answer_release(
    database, tmp_path, monkeypatch, phase
):
    value = extension(tmp_path, dependencies=True)
    if phase == "sql":
        from generaldataagent.tools.sqlite import SQLiteReadOnlyExecutor

        original = SQLiteReadOnlyExecutor.execute

        def execute(self, sql):
            result = original(self, sql)
            if sql == model_actions()[0]["sql"]:
                value.admission.registry.revoke("citation-policy", "v1")
            return result

        monkeypatch.setattr(SQLiteReadOnlyExecutor, "execute", execute)
    else:
        from generaldataagent.evaluation.stage_six_analysis import normalize_visible_answers

        def normalize(*args):
            value.admission.registry.revoke("citation-policy", "v1")
            return normalize_visible_answers(*args)

        monkeypatch.setattr(
            "generaldataagent.evaluation.stage_six_analysis.normalize_visible_answers", normalize
        )
    model = Model(*model_actions())
    result = BoundedSQLAgent(model, tool_profile="sql-analysis", skill_extension=value).run(
        case().task, database
    )
    assert result["failure"] == "skill_admission_refused"
    assert result["answers"] == {} and result["raw_answers"] is None
    assert result["answer_evidence"] == {} and result["final_sql"] is None
    assert result["tokens"] == (10 if phase == "sql" else 20)


def test_disabled_path_has_no_extension_fields_and_invalid_ports_are_refused(database):
    result = BoundedSQLAgent(Model(*model_actions())).run(case().task, database)
    assert result["failure"] is None and "skill_extension" not in result
    with pytest.raises(ValueError, match="explicit BoundedAgentSkill"):
        BoundedSQLAgent(Model(), skill_extension=object())
