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


def test_explicit_closed_form_definitions_supply_per_field_types_and_precision():
    from generaldataagent.tools.answer_fields import infer_public_field_rules

    text = (
        "@r[coefficient] @p[probability] @label[relationship]\n"
        'where "coefficient" is a number, rounded to two decimal places.\n'
        'where "probability" is a number, rounded to four decimal places.\n'
        'where "relationship" is a string, either positive or none.'
    )
    rules = infer_public_field_rules(text, ["r", "p", "label"])
    assert rules["r"].decimals == 2 and rules["p"].decimals == 4
    assert rules["label"].kind == "string"
    assert all(field.source_excerpt in text for field in rules.values())
    assert not infer_public_field_rules("@r[coefficient] no explicit type definition", ["r"])
    assert not infer_public_field_rules(text, ["hidden_reference_key"])


def test_float_with_decimal_places_is_an_explicit_format_not_a_guess():
    from generaldataagent.tools.answer_fields import infer_public_field_rules

    text = '@speed[number] where "number" is a float with two decimal places.'
    assert public_precisions(text) == {2}
    assert infer_public_field_rules(text, ["speed"])["speed"].decimals == 2
    assert validate_field_value(1, AnswerFieldSpec(kind="boolean"), {}, []) is True
    with pytest.raises(ValueError, match="boolean"):
        validate_field_value(2, AnswerFieldSpec(kind="boolean"), {}, [])


def test_one_where_introduces_several_independent_public_field_definitions():
    from generaldataagent.tools.answer_fields import infer_public_field_rules

    text = (
        "@r[coefficient] @p[probability] @label[relationship]. "
        'Where "coefficient" is a number rounded to two decimal places. '
        '"probability" is a number rounded to four decimal places. '
        '"relationship" is a string, either "positive" or "none".'
    )
    rules = infer_public_field_rules(text, ["r", "p", "label"])
    assert rules["r"].decimals == 2 and rules["p"].decimals == 4
    assert rules["label"].kind == "string"
    assert all(rule.source_excerpt in text for rule in rules.values())
    grouped = (
        '@a[first] @b[second] where "first" and "second" are numbers rounded to two decimal places.'
    )
    rules = infer_public_field_rules(grouped, ["a", "b"])
    assert set(rules) == {"a", "b"} and rules["a"].decimals == rules["b"].decimals == 2


def test_public_placeholder_can_contain_percent_symbols_without_losing_its_type():
    from generaldataagent.tools.answer_fields import infer_public_field_rules

    text = (
        '@rate[rate_%] where "rate_%" is a positive real value between 0 and 100, '
        "rounded to two decimal places."
    )
    rules = infer_public_field_rules(text, ["rate"])
    assert rules["rate"].kind == "number" and rules["rate"].decimals == 2
    assert rules["rate"].source_excerpt in text
    assert not infer_public_field_rules(text + " @rate[other]", ["rate"])


def test_public_unique_ellipsis_format_requires_a_complete_collection_not_a_string():
    from generaldataagent.tools.answer_fields import infer_public_field_rules

    text = (
        "@names[column1, column2,…] whereby the entries are string column names. "
        "The answer should not contain any duplicates and should be sorted alphabetically."
    )
    rule = infer_public_field_rules(text, ["names"])["names"]
    assert rule.kind == "set" and rule.nullable is False
    assert rule.source_excerpt == text
    assert not infer_public_field_rules("@range[low, high] without duplicates", ["range"])
    assert not infer_public_field_rules("@names[a,b,...] Preserve duplicates.", ["names"])
    assert not infer_public_field_rules(text + " @other[undefined]", ["names", "other"])


def test_explicit_set_definition_does_not_reinterpret_a_string_description():
    from generaldataagent.tools.answer_fields import infer_public_field_rules

    text = "@names[columns] where 'columns' is a set of string names."
    assert infer_public_field_rules(text, ["names"])["names"].kind == "set"
    text = "@description[text] where 'text' is a string describing a set of columns."
    assert infer_public_field_rules(text, ["description"])["description"].kind == "string"


def test_public_collection_presentation_is_sourced_and_does_not_change_raw_items():
    from generaldataagent.tools.answer_fields import (
        infer_public_field_rules,
        present_collection,
        validate_field_sources,
    )

    text = "@names[item1, item2,…] No duplicates."
    rule = infer_public_field_rules(text, ["names"])["names"]
    assert rule.collection_separator == ", "
    validate_field_sources({"names": rule}, ["names"], "", text)
    assert present_collection(["author", "image"], rule.collection_separator) == "author, image"
    assert present_collection([], rule.collection_separator) == ""
    assert present_collection(["author"], None) == ["author"]
    json_rule = infer_public_field_rules(text + " Use a JSON array.", ["names"])["names"]
    assert json_rule.collection_separator is None
    with pytest.raises(ValueError, match="explicit public format"):
        validate_field_sources(
            {"names": rule.model_copy(update={"collection_separator": ","})}, ["names"], "", text
        )
    with pytest.raises(ValueError, match="unambiguous"):
        present_collection(["field,with,commas"], ", ")
    with pytest.raises(ValueError, match="unambiguous"):
        present_collection([None], ",")
