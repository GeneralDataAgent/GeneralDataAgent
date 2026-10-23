from pathlib import Path

import pytest

from generaldataagent.business.rules.question import RuleQuestionError, parse_rule_question

MERCHANTS = ("Belles_cookbook_store", "Martinis_Fine_Steakhouse", "Toms_Kitchen")
MCC = {"Eating Places and Restaurants": 5812, "General Contractors": 1520}
EXCERPT = "null means all"


def parse(question, **changes):
    values = dict(known_merchants=MERCHANTS, semantics_excerpt=EXCERPT, mcc_descriptions=MCC)
    return parse_rule_question(question, **(values | changes))


def refusal(question, **changes):
    with pytest.raises(RuleQuestionError) as denied:
        parse(question, **changes)
    return denied.value.code


def test_merchant_month_question_becomes_an_applicability_request():
    request = parse("What are the applicable fee IDs for Belles_cookbook_store in March 2023?")
    assert request.operation == "applicable"
    assert (request.merchant, request.year, request.month) == (
        "Belles_cookbook_store",
        2023,
        3,
    )
    assert request.day_of_year is None


def test_ordinal_day_question_uses_the_day_of_year_scope():
    request = parse(
        "For the 10th of the year 2023, what are the Fee IDs applicable to Belles_cookbook_store?"
    )
    assert request.operation == "applicable"
    assert request.day_of_year == 10
    assert request.month is None


def test_rate_change_question_becomes_a_counterfactual():
    request = parse(
        "In January 2023 what delta would Belles_cookbook_store pay if the relative fee "
        "of the fee with ID=384 changed to 1?"
    )
    assert request.operation == "counterfactual"
    assert (request.rule_id, request.alternate_rate) == ("384", "1")
    assert (request.merchant, request.year, request.month) == ("Belles_cookbook_store", 2023, 1)


@pytest.mark.parametrize(
    "condition",
    [
        "credit transactions",
        "debit transactions",
        "aci = B",
        "account_type = R",
        "card scheme Aurora",
        "MCC description: Eating Places and Restaurants",
        "transaction value of 25 EUR",
        "is_credit = true",
        "intracountry = true",
    ],
)
@pytest.mark.parametrize("operation", ["applicable", "counterfactual"])
def test_contextual_questions_never_discard_explicit_facets(condition, operation):
    if operation == "applicable":
        question = (
            "Which fee IDs are applicable to Belles_cookbook_store in January 2023 "
            f"for {condition}?"
        )
    else:
        question = (
            "What delta would Belles_cookbook_store pay in January 2023 "
            f"for {condition} if fee ID=901 changed to 32?"
        )
    assert refusal(question) == "rule_question_unsupported_context_facets"


def test_average_fee_reads_only_the_facets_the_question_states():
    request = parse(
        "For credit transactions, what would be the average fee that the card scheme "
        "GlobalCard would charge for a transaction value of 10 EUR?"
    )
    assert request.operation == "average_fee"
    assert request.card_scheme == "GlobalCard"
    assert request.is_credit is True
    # Unstated facets stay unconstrained rather than being guessed.
    assert request.account_type is None
    assert request.aci is None
    assert request.merchant_category_code is None


def test_mcc_description_resolves_through_the_public_table():
    request = parse(
        "For account type H and the MCC description: Eating Places and Restaurants, what "
        "would be the average fee that the card scheme GlobalCard would charge?"
    )
    assert request.operation == "average_fee"
    assert request.account_type == "H"
    assert request.merchant_category_code == 5812


def test_unknown_mcc_description_is_refused_instead_of_widening_scope():
    assert (
        refusal(
            "For the MCC description: Not In The Table, what is the average fee that the card "
            "scheme GlobalCard would charge?"
        )
        == "rule_question_unresolved_mcc"
    )


def test_pure_facet_question_selects_without_merchant_context():
    request = parse("What is the fee ID or IDs that apply to account_type = R and aci = B?")
    assert request.operation == "select"
    assert (request.account_type, request.aci) == ("R", "B")


@pytest.mark.parametrize(
    "question,code",
    [
        ("Which issuing country has the highest number of transactions?", "unrecognised"),
        ("What is the top country (ip_country) for fraud?", "unrecognised"),
        ("What are the applicable fee IDs for Nowhere_Shop in March 2023?", "missing_merchant"),
        ("What are the applicable fee IDs for Belles_cookbook_store?", "missing_time_scope"),
        (
            "For Belles_cookbook_store in January 2023 and on the 10th, what applies?",
            "ambiguous_time_scope",
        ),
        (
            "What delta would Belles_cookbook_store pay in January 2023 if the fee changed to 1?",
            "missing_rule_id",
        ),
        (
            "What delta would Belles_cookbook_store pay in January 2023 if fee ID=384 changed?",
            "missing_alternate_rate",
        ),
        (
            "What delta would the store pay in January 2023 if fee ID=384 changed to 1?",
            "missing_merchant",
        ),
    ],
)
def test_unjustified_questions_are_refused_with_a_stable_code(question, code):
    assert refusal(question).startswith("rule_question_") and code in refusal(question)


def test_semantics_excerpt_is_required_and_never_fabricated():
    assert refusal("anything", semantics_excerpt="") == "rule_question_missing_semantics_excerpt"
    assert refusal("anything", semantics_excerpt="   ") == "rule_question_missing_semantics_excerpt"


def test_question_text_is_required():
    with pytest.raises(ValueError):
        parse_rule_question("", known_merchants=MERCHANTS, semantics_excerpt=EXCERPT)
    with pytest.raises(ValueError):
        parse_rule_question(None, known_merchants=MERCHANTS, semantics_excerpt=EXCERPT)


def test_unknown_failure_code_is_rejected():
    with pytest.raises(ValueError, match="unknown rule question failure code"):
        RuleQuestionError("not_a_code")


def test_merchant_matching_requires_exactly_one_known_name():
    # Two known merchants in one question is ambiguous, so it is refused rather
    # than resolved to the first match.
    assert (
        refusal(
            "Do Belles_cookbook_store and Toms_Kitchen apply in March 2023?",
        )
        == "rule_question_missing_merchant"
    )


def test_parser_module_does_not_read_answers_or_reference_queries():
    source = Path("src/generaldataagent/business/rules/question.py").read_text()
    for forbidden in ("expected_answer", "reference_sql", "gold", "task_id"):
        assert forbidden not in source


def test_empty_list_semantics_is_an_explicit_caller_declaration():
    question = (
        "For the 10th of the year 2023, what are the Fee IDs applicable to Belles_cookbook_store?"
    )
    closed = parse(question)
    open_list = parse(question, empty_list_is_wildcard=True)
    # The parser never infers this from the annex; it carries what it was told.
    assert closed.empty_list_is_wildcard is False
    assert open_list.empty_list_is_wildcard is True
    # Everything else about the request is identical.
    assert closed.model_dump(exclude={"empty_list_is_wildcard"}) == open_list.model_dump(
        exclude={"empty_list_is_wildcard"}
    )


@pytest.mark.parametrize(
    "suffix",
    [
        "account type X",
        "account_type =",
        "account_type = R and account type H",
        "aci = ABCDEFGHI",
        "aci = A and aci = B",
        "card scheme " + "A" * 65,
        "card scheme Aurora and card scheme Boreal",
    ],
)
def test_explicit_invalid_or_repeated_facets_are_never_dropped_or_truncated(suffix):
    assert refusal("What is the average fee for " + suffix + "?") == "rule_question_invalid_facet"


@pytest.mark.parametrize("descriptions", [None, {}, {"Shop": 1, " shop ": 2}])
def test_mcc_requires_one_resolved_code(descriptions):
    assert (
        refusal(
            "What is the average fee for MCC description: Shop?",
            mcc_descriptions=descriptions,
        )
        == "rule_question_unresolved_mcc"
    )


def test_repeated_mcc_description_is_refused():
    assert (
        refusal(
            "What is the average fee for MCC description: General Contractors, "
            "MCC description: Eating Places and Restaurants?"
        )
        == "rule_question_unresolved_mcc"
    )


def test_field_marker_does_not_match_inside_a_scheme_name():
    request = parse("What is the average fee for card scheme Acico?")
    assert request.card_scheme == "Acico"
    assert request.aci is None


@pytest.mark.parametrize(
    "rule_id",
    ["901.5", "901e2", "901,902", "901/902", "9" * 129],
)
def test_counterfactual_rule_id_is_never_read_as_a_numeric_prefix(rule_id):
    assert (
        refusal(
            f"What delta would Belles_cookbook_store pay in January 2023 "
            f"if fee ID={rule_id} changed to 1?"
        )
        == "rule_question_invalid_rule_id"
    )


@pytest.mark.parametrize("rate", ["1%", "1e2", "1,000", "1/2", "1.2.3", "-1", "NaN", "9" * 41])
def test_counterfactual_rate_requires_one_complete_supported_numeric_value(rate):
    assert (
        refusal(
            f"What delta would Belles_cookbook_store pay in January 2023 "
            f"if fee ID=901 changed to {rate}?"
        )
        == "rule_question_invalid_alternate_rate"
    )


@pytest.mark.parametrize(
    "clause,code",
    [
        ("fee ID=901 and ID=902 changed to 1", "rule_question_invalid_rule_id"),
        ("fee ID=901 changed to 1 and changed to 2", "rule_question_invalid_alternate_rate"),
    ],
)
def test_counterfactual_multiple_declarations_are_refused(clause, code):
    assert (
        refusal(f"What delta would Belles_cookbook_store pay in January 2023 if {clause}?") == code
    )


@pytest.mark.parametrize("verb", ["changed", "changes", "change"])
@pytest.mark.parametrize("rate", ["0", "0.125", "32"])
def test_counterfactual_preserves_complete_decimal_rate(verb, rate):
    request = parse(
        f"What delta would Belles_cookbook_store pay in January 2023 "
        f"if fee id=901 {verb} to {rate}?"
    )
    assert request.operation == "counterfactual"
    assert (request.rule_id, request.alternate_rate) == ("901", rate)


@pytest.mark.parametrize(
    "scope,code",
    [
        ("day 1 and day 2 of 2024", "ambiguous_time_scope"),
        ("the 1st and the 2nd of 2024", "ambiguous_time_scope"),
        ("day 1 and the 1st of 2024", "ambiguous_time_scope"),
        ("day 0 of 2024", "invalid_time_scope"),
        ("day 367 of 2024", "invalid_time_scope"),
        ("day 1000 of 2024", "invalid_time_scope"),
        ("day 1.5 of 2024", "invalid_time_scope"),
        ("day 1,2 of 2024", "invalid_time_scope"),
        ("day 1/2 of 2024", "invalid_time_scope"),
        ("day -1 of 2024", "invalid_time_scope"),
        ("day 366 of 2023", "invalid_time_scope"),
        ("January 10, 2024", "invalid_time_scope"),
        ("January 2024 and day 400", "invalid_time_scope"),
    ],
)
def test_time_scope_never_selects_a_prefix_or_discards_an_invalid_day(scope, code):
    assert (
        refusal(f"Which fee IDs are applicable to Belles_cookbook_store on {scope}?")
        == "rule_question_" + code
    )


@pytest.mark.parametrize(
    "scope,day",
    [
        ("day 366 of 2024", 366),
        ("60th of the year 2024", 60),
        ("day 365 of 2023", 365),
        ("the 10th of 2023", 10),
    ],
)
def test_supported_day_scope_keeps_the_exact_calendar_day(scope, day):
    request = parse(f"Which fee IDs are applicable to Belles_cookbook_store on {scope}?")
    assert request.day_of_year == day
    assert request.month is None


def test_empty_list_semantics_must_be_a_boolean():
    with pytest.raises(ValueError, match="boolean declaration"):
        parse("anything", empty_list_is_wildcard="true")
