import pytest

from generaldataagent.evaluation.repair_budget import RepairBudget
from generaldataagent.evaluation.routing import failure_cause, route_task, should_retry


def test_rule_questions_take_the_governed_lane():
    route = route_task("What is the fee for merchant X in January 2023?")
    assert route.lane == "governed"
    assert route.tool_profile == "sql-rules"
    assert route.signals == ("fee",)
    assert (route.max_steps, route.max_repairs) == (8, 3)


def test_statistical_questions_take_the_analysis_lane():
    route = route_task("Is the correlation between amount and fraud significant?")
    assert route.lane == "analysis"
    assert route.tool_profile == "sql-science"
    assert "correlation" in route.signals
    assert "significant" in route.signals


def test_plain_questions_take_the_cheapest_lane():
    route = route_task("How many payments were made in 2023?")
    assert route.lane == "short"
    assert route.signals == ()
    assert route.max_steps == 5


def test_governed_outranks_analysis_when_both_signals_appear():
    route = route_task("Test the correlation of fee rate against transaction amount")
    assert route.lane == "governed"
    assert set(route.signals) >= {"fee", "rate"}


def test_public_guidelines_are_part_of_the_routing_text():
    route = route_task("Which merchants qualify?", instructions="Use the fee manual.")
    assert route.lane == "governed"


def test_routing_is_case_insensitive_and_deterministic():
    first = route_task("WHAT IS THE FEE?")
    second = route_task("what is the fee?")
    assert first == second
    assert first.lane == "governed"


def test_operator_override_is_honoured_and_recorded():
    route = route_task("How many payments?", override="analysis")
    assert route.lane == "analysis"
    assert route.signals == ("operator_override",)


@pytest.mark.parametrize(
    "args",
    [
        dict(question=""),
        dict(question="   "),
        dict(question="q", instructions=None),
        dict(question="q", override="expensive"),
    ],
)
def test_invalid_routing_inputs_are_rejected(args):
    with pytest.raises(ValueError):
        route_task(args.pop("question"), **args)


def test_route_serialises_for_the_run_record():
    payload = route_task("median transaction amount").as_dict()
    assert payload["lane"] == "analysis"
    assert payload["tool_profile"] == "sql-science"
    assert payload["signals"] == ["median"]


@pytest.mark.parametrize(
    "code,cause,retry",
    [
        ("unknown_column", "parse", True),
        ("action_schema", "parse", True),
        ("rule_context_required", "parse", True),
        ("semantic_contract", "compute", True),
        ("unsupported_function", "compute", True),
        ("missing_evidence", "evidence", True),
        ("unsupported_capability", "capability", False),
        ("policy_rejection", "capability", False),
        ("multiple_applicable_rules", "ambiguity", False),
        ("time_budget", "resource", False),
        ("repair_budget_exhausted", "resource", False),
        # A capped or timed-out query is narrowed, not abandoned.
        ("query_timeout", "compute", True),
        ("result_limit", "compute", True),
        ("a_brand_new_code", "parse", True),
    ],
)
def test_failure_causes_decide_whether_a_retry_is_worth_it(code, cause, retry):
    assert failure_cause(code) == cause
    assert should_retry(code) is retry


def test_failure_classification_requires_a_code():
    with pytest.raises(ValueError):
        failure_cause("")
    with pytest.raises(ValueError):
        should_retry(None)


@pytest.mark.parametrize("code", ["unsupported_capability", "multiple_applicable_rules"])
def test_capability_and_ambiguity_failures_consume_no_repair_budget(code):
    budget = RepairBudget(max_repairs=3, max_identical=2)
    decision = budget.request(code, tool="sql")
    assert not decision.allowed
    assert decision.reason.startswith("repair_not_applicable_")
    assert budget.attempted == 0
    assert budget.report()["exhausted"] is False


def test_retryable_failures_still_consume_budget():
    budget = RepairBudget(max_repairs=2, max_identical=2)
    assert budget.request("unknown_column", tool="sql").allowed
    assert budget.request("semantic_contract", tool="sql").allowed
    assert not budget.request("sql_syntax", tool="sql").allowed
    assert budget.attempted == 2


@pytest.mark.parametrize(
    "question,expected_lane",
    [
        # "fee" is a substring of these, but none of them is a fee question.
        ("How many transactions were at coffee shops in 2023?", "short"),
        ("List the feedback comments for merchant Cedar_Lantern", "short"),
        # "rate" is a substring of these.
        ("How many accurate transactions were recorded?", "short"),
        ("Count payments for corporate accounts", "short"),
        ("How many transactions did we generate per merchant?", "short"),
        # "manual", "fee" and "rate" fragments inside longer words.
        ("Was the payment made manually or automatically?", "short"),
        ("Show the refunded transactions", "short"),
    ],
)
def test_markers_match_whole_words_only(question, expected_lane):
    route = route_task(question)
    assert route.lane == expected_lane
    assert route.signals == ()


@pytest.mark.parametrize(
    "question,expected_lane,expected_signal",
    [
        ("What are the interchange rates for this merchant?", "governed", "interchange"),
        ("What are the monthly fees?", "governed", "fees"),
        ("Which merchants have a per-transaction threshold?", "governed", "threshold"),
        ("Is the median amount correlated with the fee?", "governed", "fee"),
        ("Compare the p-value distribution", "analysis", "p-value"),
        ("Show the standard deviations", "analysis", "standard deviation"),
    ],
)
def test_plural_forms_and_phrases_still_match(question, expected_lane, expected_signal):
    route = route_task(question)
    assert route.lane == expected_lane
    assert expected_signal in route.signals


def test_a_marker_inside_a_column_name_is_not_public_prose_evidence():
    # Underscored identifiers are prose the parser writes, not the published
    # question, so they must not decide the lane.
    route = route_task("Show fee_rate, manual_flag and transaction_count per day")
    assert route.lane == "short"
    assert route.signals == ()
