import json
from types import SimpleNamespace

import pytest

from generaldataagent.evaluation.repair_budget import RepairBudget
from generaldataagent.evaluation.stage_six import BoundedSQLAgent, Task


def test_budget_counts_distinct_repairs_and_reports_telemetry():
    budget = RepairBudget(max_repairs=3, max_identical=2)
    assert budget.request("unknown_column", tool="sql").allowed
    assert budget.request("unknown_table", tool="sql").allowed
    assert budget.request("unknown_column", tool="sql").allowed
    decision = budget.request("sql_syntax", tool="sql")
    assert not decision.allowed
    assert decision.reason == "repair_budget_exhausted"
    report = budget.report()
    assert report["attempted"] == 3
    assert report["by_code"] == {"unknown_column": 2, "unknown_table": 1}
    assert report["exhausted"] is True
    assert report["refused"] == ["sql_syntax::sql"]


def test_identical_failure_is_refused_before_the_total_budget_runs_out():
    budget = RepairBudget(max_repairs=10, max_identical=2)
    assert budget.request("rule_context_required", tool="rules").allowed
    assert budget.request("rule_context_required", tool="rules").allowed
    decision = budget.request("rule_context_required", tool="rules")
    assert not decision.allowed
    assert decision.reason == "repair_repeated_identical"
    assert budget.attempted == 2
    # A different cause still has budget.
    assert budget.request("rule_month_empty", tool="rules").allowed


def test_zero_budget_refuses_the_first_repair():
    budget = RepairBudget(max_repairs=0)
    decision = budget.request("unknown_column")
    assert not decision.allowed
    assert decision.reason == "repair_budget_exhausted"
    assert budget.attempted == 0


@pytest.mark.parametrize(
    "kwargs",
    [dict(max_repairs=-1), dict(max_repairs=True), dict(max_identical=0), dict(max_identical=1.5)],
)
def test_invalid_budgets_are_rejected(kwargs):
    with pytest.raises(ValueError):
        RepairBudget(**kwargs)


def test_budget_requires_a_stable_code():
    with pytest.raises(ValueError):
        RepairBudget().request("   ")


def test_agent_rejects_invalid_repair_budgets():
    model = SimpleNamespace(bind=lambda **kwargs: SimpleNamespace(invoke=lambda m: None))
    with pytest.raises(ValueError, match="invalid repair budget"):
        BoundedSQLAgent(model, max_repairs=-1)
    with pytest.raises(ValueError, match="invalid repair budget"):
        BoundedSQLAgent(model, max_identical_repairs=0)
    agent = BoundedSQLAgent(model, tool_profile="sql-rules", max_repairs=1)
    assert (agent.max_repairs, agent.max_identical_repairs) == (1, 2)


class ScriptedModel:
    """Return a fixed sequence of tool calls, then repeat the last one."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def bind(self, **kwargs):
        return self

    def invoke(self, messages):
        self.calls += 1
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return SimpleNamespace(
            content="",
            tool_calls=[reply],
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )


def task(database):
    return Task(
        case_id="budget-1",
        benchmark="dabstep",
        question="How many payments are there?",
        instructions="Answer with a number under answer.",
        difficulty="easy",
        asset=str(database),
    )


def rule_database(tmp_path):
    import sqlite3

    path = tmp_path / "rules.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE fees (payload TEXT)")
        db.execute("INSERT INTO fees VALUES (?)", (json.dumps({"ID": "A"}),))
    return path


def test_agent_stops_when_the_same_rule_failure_repeats(tmp_path):
    from generaldataagent.evaluation.stage_nine import RuleToolError

    database = rule_database(tmp_path)
    # The excerpt is never in the supplied documents, so every call fails the
    # same way; the second repeat must end the task rather than spend steps.
    call = {
        "name": "analyze_fee_rules",
        "args": {"operation": "select", "semantics_excerpt": "not in documents"},
    }
    model = ScriptedModel([call])
    agent = BoundedSQLAgent(
        model,
        max_steps=6,
        tool_profile="sql-rules",
        max_repairs=3,
        max_identical_repairs=2,
    )
    result = agent.run(task(database), database, documents="null means all")
    assert result["failure"] == "repair_budget_exhausted"
    assert result["repair_budget"]["exhausted"] is True
    assert result["repair_budget"]["attempted"] == 2
    assert result["repair_budget"]["refused"] == ["rule_semantics_excerpt_missing::rules"]
    assert model.calls == 3
    refused = [row for row in result["trace"] if row.get("status") == "repair_refused"]
    assert refused and refused[0]["code"] == "repair_repeated_identical"
    assert RuleToolError("rule_semantics_excerpt_missing").code == "rule_semantics_excerpt_missing"


def test_non_repair_profile_reports_an_empty_budget(tmp_path):
    database = rule_database(tmp_path)
    model = ScriptedModel(
        [{"name": "declare_unsupported", "args": {"reason": "not answerable from the database"}}]
    )
    result = BoundedSQLAgent(model, max_steps=2, tool_profile="sql").run(task(database), database)
    assert result["repair_budget"] == {
        "attempted": 0,
        "max_repairs": 0,
        "max_identical": 2,
        "by_code": {},
        "refused": [],
        "exhausted": False,
    }


@pytest.mark.parametrize("code", ["policy_rejection", "multiple_applicable_rules", "time_budget"])
def test_nonrepairable_failure_does_not_claim_repair_budget_exhaustion(code):
    budget = RepairBudget(max_repairs=3)
    decision = budget.request(code, tool="rules")
    assert not decision.allowed
    assert decision.reason.startswith("repair_not_applicable_")
    assert budget.report()["attempted"] == 0
    assert budget.report()["exhausted"] is False
    assert budget.report()["refused"] == [f"{code}::rules"]


def test_agent_policy_rejection_preserves_cause_and_stops_before_experience(tmp_path, monkeypatch):
    from generaldataagent.evaluation import stage_nine

    database = rule_database(tmp_path)
    hints_requested = []

    def deny(*args, **kwargs):
        raise PermissionError("synthetic operation not allowed")

    def experience(*args):
        hints_requested.append(args)
        return ()

    monkeypatch.setattr(stage_nine, "analyze_fee_rules", deny)
    model = ScriptedModel(
        [
            {
                "name": "analyze_fee_rules",
                "args": {"operation": "select", "semantics_excerpt": "null"},
            }
        ]
    )
    result = BoundedSQLAgent(
        model, tool_profile="sql-rules", max_steps=6, experience=experience
    ).run(task(database), database, documents="null means all")
    assert result["failure"] == "policy_rejection"
    assert result["repair_budget"]["attempted"] == 0
    assert result["repair_budget"]["exhausted"] is False
    assert model.calls == 1
    assert hints_requested == []
    assert result["experience_used"] == []
    assert result["answers"] == {}
    assert result["tokens"] == 2
    refused = [row for row in result["trace"] if row.get("status") == "repair_refused"]
    assert refused[0]["code"] == "repair_not_applicable_capability"
    assert refused[0]["failure_code"] == "policy_rejection"
    from generaldataagent.evaluation.stage_six import summarize

    summary = summarize(
        [result | dict(case_id="denied", passed=False, difficulty="easy", sub_hits=0, sub_total=1)]
    )
    assert summary["failures"] == {"policy_rejection": 1}
    assert summary["repair_budget"]["exhausted_n"] == 0
    assert summary["repair_budget"]["attempted"] == 0


@pytest.mark.parametrize("code", ["rule_source_record_invalid", "payments_invalid_row"])
def test_source_failures_stop_without_spending_repair_budget(code):
    budget = RepairBudget(max_repairs=3)
    decision = budget.request(code, tool="rules")
    assert not decision.allowed
    assert decision.reason == "repair_not_applicable_source"
    assert budget.report()["attempted"] == 0
    assert budget.report()["exhausted"] is False
    assert budget.report()["refused"] == [f"{code}::rules"]


def test_agent_corrupt_rule_source_preserves_cause_and_skips_experience(tmp_path):
    database = rule_database(tmp_path)
    # The stored annex has only ID: the remaining required fields are missing.
    model = ScriptedModel(
        [
            {
                "name": "analyze_fee_rules",
                "args": {"operation": "select", "semantics_excerpt": "null"},
            }
        ]
    )
    hints_requested = []

    def experience(*args):
        hints_requested.append(args)
        return ()

    result = BoundedSQLAgent(
        model, tool_profile="sql-rules", max_steps=6, experience=experience
    ).run(
        task(database),
        database,
        documents="null means all\nfee = fixed_amount + rate * transaction_value / 10000",
    )
    assert result["failure"] == "rule_source_record_invalid"
    assert model.calls == 1
    assert result["tokens"] == 2
    assert result["answers"] == {}
    assert hints_requested == []
    assert result["experience_used"] == []
    assert result["repair_budget"]["attempted"] == 0
    assert result["repair_budget"]["exhausted"] is False
    refused = [row for row in result["trace"] if row.get("status") == "repair_refused"]
    assert refused[0]["code"] == "repair_not_applicable_source"
    assert refused[0]["failure_code"] == "rule_source_record_invalid"

    from generaldataagent.evaluation.stage_six import summarize

    summary = summarize(
        [
            result
            | dict(
                case_id="source-invalid",
                passed=False,
                difficulty="easy",
                sub_hits=0,
                sub_total=1,
            )
        ]
    )
    assert summary["failures"] == {"rule_source_record_invalid": 1}
    assert summary["repair_budget"]["attempted"] == 0
    assert summary["repair_budget"]["exhausted_n"] == 0


def test_nonrepairable_refusal_does_not_erase_an_earlier_ceiling_refusal():
    budget = RepairBudget(max_repairs=1)
    assert budget.request("unknown_column", tool="sql").allowed
    assert budget.request("unknown_table", tool="sql").reason == "repair_budget_exhausted"
    assert (
        budget.request("policy_rejection", tool="sql").reason == "repair_not_applicable_capability"
    )
    assert budget.report()["exhausted"] is True
    assert budget.attempted == 1


def test_run_summary_aggregates_repair_budgets():
    from generaldataagent.evaluation.stage_six import summarize

    def row(case_id, budget):
        return {
            "case_id": case_id,
            "passed": False,
            "difficulty": "easy",
            "elapsed_seconds": 1.0,
            "tokens": 1,
            "failure": "repair_budget_exhausted",
            "sub_hits": 0,
            "sub_total": 1,
            "repair_budget": budget,
        }

    results = [
        row(
            "a",
            {
                "attempted": 2,
                "max_repairs": 3,
                "max_identical": 2,
                "by_code": {"rule_semantics_excerpt_missing": 2},
                "refused": ["rule_semantics_excerpt_missing::rules"],
                "exhausted": True,
            },
        ),
        row(
            "b",
            {
                "attempted": 1,
                "max_repairs": 3,
                "max_identical": 2,
                "by_code": {"unknown_column": 1},
                "refused": [],
                "exhausted": False,
            },
        ),
    ]
    summary = summarize(results)
    assert summary["repair_budget"] == {
        "attempted": 3,
        "exhausted_n": 1,
        "by_code": {"rule_semantics_excerpt_missing": 2, "unknown_column": 1},
    }
    # Runs without the field keep the summary shape unchanged.
    for item in results:
        del item["repair_budget"]
    assert "repair_budget" not in summarize(results)
