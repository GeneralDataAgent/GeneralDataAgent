"""Per-field precision and method checks preserve full computed values."""

import pytest

from generaldataagent.contracts.answer_fields import AnswerFieldSpec
from generaldataagent.tools.answer_fields import (
    format_decimal,
    public_precisions,
    validate_field_sources,
    validate_field_value,
)


def test_public_precision_is_explicit_and_can_differ_by_field():
    assert public_precisions(
        "Round mean to two decimal places. Round total to three decimal places."
    ) == {2, 3}
    assert public_precisions("保留两位小数") == {2}
    assert not public_precisions("Example 1.23; row 3 has missing values")
    fields = {
        "mean": AnswerFieldSpec(
            kind="number", decimals=2, source_excerpt="Round mean to two decimal places."
        ),
        "total": AnswerFieldSpec(
            kind="number", decimals=3, source_excerpt="Round total to three decimal places."
        ),
    }
    validate_field_sources(
        fields,
        ["mean", "total"],
        "",
        "Round mean to two decimal places. Round total to three decimal places.",
    )
    with pytest.raises(ValueError, match="explicit"):
        validate_field_sources(
            {"n": AnswerFieldSpec(kind="number", decimals=2)}, ["n"], "count?", ""
        )


def test_types_and_sample_std_methods_are_not_guessed():
    result = {"operation": "describe"}
    sample = AnswerFieldSpec(kind="number", method="describe.sample_std")
    assert validate_field_value(4.2, sample, result, ["statistics", "x", "sample_std"]) == 4.2
    with pytest.raises(ValueError, match="sample"):
        validate_field_value(4.2, sample, result, ["statistics", "x", "mean"])
    with pytest.raises(ValueError, match="nonintegral"):
        validate_field_value(1.5, AnswerFieldSpec(kind="integer"), {}, [])
    assert validate_field_value(2.0, AnswerFieldSpec(kind="integer"), {}, []) == 2
    with pytest.raises(ValueError, match="string"):
        validate_field_value(2, AnswerFieldSpec(kind="string"), {}, [])


def test_large_finite_values_keep_explicit_precision_without_decimal_context_failure():
    assert format_decimal(1e30, 2) == "1000000000000000000000000000000.00"
    assert format_decimal(1.2345, 3) == "1.234"
    assert format_decimal(1.2355, 3) == "1.236"


def test_assembly_has_independent_precision_and_provenance_receipts():
    from generaldataagent.contracts.analysis import AnalysisTaskSpec
    from generaldataagent.orchestration.analysis_plan import accept_contract
    from generaldataagent.tools.answer_fields import binding_receipts
    from generaldataagent.tools.sql_semantics import assemble_answers

    text = "Round mean to two decimal places. Round total to three decimal places."
    spec = AnalysisTaskSpec(
        question="mean and total?",
        public_requirements=text,
        answer={
            "columns": ["mean", "total"],
            "fields": {
                "mean": {
                    "kind": "number",
                    "decimals": 2,
                    "method": "sql",
                    "source_excerpt": "Round mean to two decimal places.",
                },
                "total": {
                    "kind": "number",
                    "decimals": 3,
                    "method": "sql",
                    "source_excerpt": "Round total to three decimal places.",
                },
            },
        },
    )
    spec = accept_contract(spec.model_dump(), question=spec.question, instructions=text)
    evidence = {1: {"rows": [[1.235, 3.12345]], "method_id": "sql"}}
    bindings = {"mean": (1, ["rows", 0, 0]), "total": (1, ["rows", 0, 1])}
    answers = assemble_answers(bindings, evidence, spec.answer)
    assert answers == {"mean": "1.24", "total": "3.123"}
    receipts = binding_receipts(bindings, evidence, answers, spec.answer.fields)
    assert receipts["mean"]["raw_value"] == 1.235
    assert receipts["mean"]["method_id"] == "sql"
    assert receipts["mean"]["result_hash"] == receipts["total"]["result_hash"]
