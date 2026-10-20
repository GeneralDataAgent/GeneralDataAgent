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
    assert [t["function"]["name"] for t in planning["tools"]] == ["plan_analysis"]
    plan_schema = planning["tools"][0]["function"]["parameters"]
    assert "question" not in plan_schema["properties"]
    execution = bind_typed_tools(Model(), sql_output=True, planned=True)
    assert {t["function"]["name"] for t in execution["tools"]} == {
        "execute_sql",
        "submit_sql",
        "crosscheck_sql",
    }
    for tool in execution["tools"][:2]:
        assert tool["function"]["parameters"]["required"] == ["sql"]


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
