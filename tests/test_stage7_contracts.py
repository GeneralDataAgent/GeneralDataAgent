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
