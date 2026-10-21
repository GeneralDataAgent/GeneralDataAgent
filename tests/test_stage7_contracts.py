"""Fail closed on malformed actions and altered public requirements."""

import pytest
from pydantic import ValidationError

from generaldataagent.contracts.analysis import AnalysisTaskSpec, AnswerSpec
from generaldataagent.orchestration.analysis_plan import (
    ActionSchemaError,
    accept_contract,
    validate_action,
)


@pytest.mark.parametrize(
    "action",
    [
        {"sql": 12},
        {"sql": "select 1", "search": "secret"},
        {"sql": "select 1", "credentials": "secret"},
        {"analysis": {"table": "x", "columns": ["y"], "operation": "pearson"}},
        {"answers": {}, "evidence_steps": [True]},
        {"sql": ""},
    ],
)
def test_invalid_actions_have_redacted_error_paths(action):
    with pytest.raises(ActionSchemaError) as caught:
        validate_action(action)
    assert "secret" not in str(caught.value.paths)
    assert caught.value.paths


def test_task_text_is_preserved_and_conflicts_are_blocked():
    spec = AnalysisTaskSpec(question="which entities?", public_requirements="all ties")
    assert (
        accept_contract(spec.model_dump(), question=spec.question, instructions="all ties") == spec
    )
    with pytest.raises(ValueError, match="preserve"):
        accept_contract(spec.model_dump(), question="different", instructions="all ties")
    with pytest.raises(ValueError, match="conflicts"):
        accept_contract(
            spec.model_copy(update={"conflicts": ["ties"]}).model_dump(),
            question=spec.question,
            instructions="all ties",
        )
    with pytest.raises(ValidationError):
        AnswerSpec(columns=["id", "id"])


def test_public_rounding_overrides_omission_but_never_conflicting_precision():
    proposal = {
        "question": "correlation?",
        "public_requirements": "Round the result to two decimal places.",
    }
    spec = accept_contract(
        proposal, question=proposal["question"], instructions=proposal["public_requirements"]
    )
    assert spec.answer.decimals == 2
    proposal["answer"] = {"decimals": 3}
    with pytest.raises(ValueError, match="precision"):
        accept_contract(
            proposal, question=proposal["question"], instructions=proposal["public_requirements"]
        )


def test_stage_specific_native_schemas_do_not_offer_unavailable_tools():
    from generaldataagent.evaluation.stage_seven import bind_typed_tools

    class Model:
        def bind(self, **kwargs):
            return kwargs

    planning = bind_typed_tools(Model(), sql_output=True)
    assert {t["function"]["name"] for t in planning["tools"]} == {"submit_sql"}
    submit_schema = planning["tools"][0]["function"]["parameters"]
    assert submit_schema["required"] == ["sql", "output_columns"]
    assert "$defs" not in submit_schema
    execution = bind_typed_tools(Model(), sql_output=True, planned=True)
    assert execution["tool_choice"] == "required"
    assert {t["function"]["name"] for t in execution["tools"]} == {
        "execute_sql",
        "submit_sql",
        "crosscheck_sql",
        "declare_unsupported",
    }
    for tool in execution["tools"][:2]:
        assert tool["function"]["parameters"]["required"] == ["sql"]
    optional = bind_typed_tools(Model(), sql_output=True, plan_enabled=True)
    schema = next(
        t["function"]["parameters"]
        for t in optional["tools"]
        if t["function"]["name"] == "submit_query_plan"
    )
    assert "source_excerpt" not in schema["properties"]
    assert "source_excerpt" not in schema["required"]
    assert "source_excerpt" in schema["$defs"]["PlanFilter"]["required"]


def test_null_unused_native_fields_do_not_turn_into_multiple_actions():
    action = validate_action({"sql": "SELECT 1", "final_sql": None, "contract": None})
    assert action == {"sql": "SELECT 1"}


def test_json_function_envelopes_are_normalized_without_discarding_actions():
    from generaldataagent.orchestration.analysis_plan import normalize_protocol_action

    assert normalize_protocol_action({"submit_sql": {"sql": "SELECT 1"}}) == {
        "final_sql": "SELECT 1"
    }
    mixed = {"submit_sql": {"sql": "SELECT 1"}, "execute_sql": {"sql": "SELECT 2"}}
    assert normalize_protocol_action(mixed) == mixed
    with pytest.raises(ActionSchemaError):
        validate_action(normalize_protocol_action(mixed))


def test_precision_requires_an_explicit_public_rounding_rule():
    proposal = {"question": "correlation?", "answer": {"decimals": 2}}
    with pytest.raises(ValueError, match="public"):
        accept_contract(proposal, question="correlation?", instructions="")


def test_flat_native_bindings_reject_duplicates_and_boolean_step_ids():
    from generaldataagent.orchestration.analysis_plan import normalize_protocol_action

    good = {"submit_answers": {"fields": [{"name": "n", "step": 1, "path": ["rows", 0, 0]}]}}
    assert validate_action(normalize_protocol_action(good))["bindings"] == {
        "n": (1, ["rows", 0, 0])
    }
    for fields in [
        [{"name": "n", "step": True, "path": ["rows", 0, 0]}],
        [{"name": "n", "step": 1, "path": ["rows", 0, 0]}] * 2,
    ]:
        with pytest.raises(ActionSchemaError):
            validate_action(normalize_protocol_action({"submit_answers": {"fields": fields}}))


def test_native_optional_requirements_advertise_strict_fields_and_required_public_sources():
    from generaldataagent.evaluation.stage_seven import bind_typed_tools

    class Model:
        def bind(self, **kwargs):
            return kwargs

    def requirements(tool):
        return tool["function"]["parameters"]["properties"]["requirements"]

    wire = bind_typed_tools(Model(), sql_output=True, plan_enabled=True)
    selected = {t["function"]["name"]: t for t in wire["tools"]}
    for name in ["submit_sql", "plan_analysis"]:
        schema = requirements(selected[name])
        assert schema["additionalProperties"] is False
        assert set(schema["properties"]) == {"predicates", "dates", "ratios"}
        for kind in ["predicates", "dates", "ratios"]:
            item = schema["properties"][kind]["items"]
            assert item["additionalProperties"] is False
            assert "source_excerpt" in item["required"]
        assert "$ref" not in str(schema) and "$defs" not in str(schema)
        assert "requirements" not in selected[name]["function"]["parameters"]["required"]


def test_compact_sql_wire_keeps_basic_actions_and_full_requirements_stay_opt_in():
    import json

    from generaldataagent.evaluation.stage_seven import bind_typed_tools

    class Model:
        def bind(self, **kwargs):
            return kwargs

    compact = bind_typed_tools(Model(), sql_output=True, exploration_enabled=True, compact_sql=True)
    rich = bind_typed_tools(Model(), sql_output=True, exploration_enabled=True)
    assert compact["tool_choice"] == "required"
    tools = {t["function"]["name"]: t["function"]["parameters"] for t in compact["tools"]}
    assert set(tools) == {"submit_sql", "execute_sql", "declare_unsupported"}
    assert set(tools["submit_sql"]["properties"]) == {"sql", "output_columns", "shape", "ties"}
    assert len(json.dumps(compact["tools"])) < len(json.dumps(rich["tools"])) * 0.5
    advanced = bind_typed_tools(Model(), sql_output=True, plan_enabled=True)
    assert any(
        "requirements" in t["function"]["parameters"]["properties"] for t in advanced["tools"]
    )


def test_model_rules_cannot_downgrade_known_public_types_or_swap_field_precisions():
    text = (
        "@a[first] @b[second] where 'first' is a number with two decimal places. "
        "'second' is a number with four decimal places."
    )
    proposal = {
        "question": "compute",
        "public_requirements": text,
        "answer": {
            "columns": ["a", "b"],
            "fields": {"a": {"kind": "number"}, "b": {"kind": "number"}},
        },
    }
    spec = accept_contract(proposal, question="compute", instructions=text)
    assert spec.answer.fields["a"].decimals == 2 and spec.answer.fields["b"].decimals == 4
    proposal["answer"]["fields"]["a"] = {
        "kind": "number",
        "decimals": 4,
        "source_excerpt": "'second' is a number with four decimal places.",
    }
    with pytest.raises(ValueError, match="precision contradicts"):
        accept_contract(proposal, question="compute", instructions=text)
    text = "@items[a,b,...] The answer should not contain any duplicates."
    proposal = {
        "question": "list",
        "public_requirements": text,
        "answer": {"columns": ["items"], "fields": {"items": {"kind": "string"}}},
    }
    with pytest.raises(ValueError, match="type contradicts"):
        accept_contract(proposal, question="list", instructions=text)
    proposal["answer"]["fields"]["items"] = {"kind": "set", "nullable": True}
    with pytest.raises(ValueError, match="nullability contradicts"):
        accept_contract(proposal, question="list", instructions=text)


def test_known_public_type_does_not_admit_fabricated_source_excerpt():
    text = "@flag[value] where 'value' is a boolean."
    proposal = {
        "question": "flag",
        "public_requirements": text,
        "answer": {
            "columns": ["flag"],
            "fields": {"flag": {"kind": "boolean", "source_excerpt": "invented source"}},
        },
    }
    with pytest.raises(ValueError, match="exact public source"):
        accept_contract(proposal, question="flag", instructions=text)
