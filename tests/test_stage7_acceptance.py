"""Full pairs and historical correctness cannot be claimed from a small or changed cohort."""

import copy

import pytest

from generaldataagent.evaluation.stage_seven_acceptance import historical_protection, qualify_stage7
from generaldataagent.evaluation.stage_six import summarize


def report():
    rows = [
        {
            "case_id": str(i),
            "passed": True,
            "failure": None,
            "difficulty": "easy",
            "elapsed_seconds": 1.0,
            "tokens": 10,
            "sub_hits": 1,
            "sub_total": 1,
            "cost_usd": None,
            "usage_status": "complete",
            "database_hash": "db",
        }
        for i in range(2)
    ]
    return {
        "benchmark": "bird-mini-dev-sqlite",
        "cohort_hash": "c",
        "protocol_hash": "p",
        "source_manifest_hash": "s",
        "runner_hash": "r",
        "model": "m",
        "results": rows,
        "summary": summarize(rows),
    }


def test_historical_correct_guard_preserves_all_rows_and_does_not_claim_causal_attribution():
    before = report()
    after = copy.deepcopy(before)
    after["protocol_hash"] = "new_protocol"
    after["results"][1].update(passed=False, failure="answer_mismatch", sub_hits=0)
    after["summary"] = summarize(after["results"])
    guard = historical_protection(before, after)
    assert guard["n"] == 2 and guard["regressions"] == ["1"]
    assert not guard["passed"] and not guard["causal_attribution"]
    assert not guard["protocols_equal"]
    after["results"][1]["database_hash"] = "different"
    with pytest.raises(ValueError, match="database"):
        historical_protection(before, after)


def test_small_sample_or_missing_track_cannot_close_full_acceptance():
    with pytest.raises(ValueError, match="all three"):
        qualify_stage7([report()], [report()], [report()])
    with pytest.raises(ValueError, match="all three"):
        qualify_stage7([report()] * 3, [report()] * 3, [report()] * 3)


def test_receipt_hash_does_not_certify_a_fabricated_sql_value_or_method():
    from generaldataagent.evaluation.stage_seven_acceptance import answer_receipt_errors

    data = {
        "benchmark": "infiagent-dabench",
        "results": [
            {
                "case_id": "synthetic",
                "passed": True,
                "answers": {"mean": 0.99},
                "answer_evidence": {
                    "mean": {
                        "step": 1,
                        "path": ["rows", 0, 0],
                        "method_id": "sql",
                        "result_hash": "same",
                        "submitted_value": 0.99,
                    }
                },
                "trace": [
                    {
                        "step": 1,
                        "status": "ok",
                        "tool": "sql",
                        "result_hash": "same",
                        "sql": "WITH a AS (SELECT 0.99 AS x) SELECT x FROM a",
                    }
                ],
            }
        ],
    }
    assert answer_receipt_errors(data) == ["synthetic:unverified_sql_data_dependency"]
    data["results"][0]["trace"][0]["sql"] = "SELECT AVG(value) FROM measurements"
    assert not answer_receipt_errors(data)
    data["results"][0]["answer_evidence"]["mean"]["method_id"] = "pearson"
    assert answer_receipt_errors(data) == ["synthetic:receipt_method_mismatch"]


def test_sql_output_receipts_require_matching_execution_and_every_column_dependency():
    from generaldataagent.evaluation.stage_seven_acceptance import answer_receipt_errors

    sql = "SELECT COUNT(*) AS n FROM people"
    row = {
        "case_id": "synthetic",
        "passed": True,
        "final_sql": sql,
        "trace": [{"step": 1, "tool": "sql", "status": "ok", "sql": sql, "result_hash": "a" * 64}],
    }
    report = {"benchmark": "bird-mini-dev-sqlite", "results": [row]}
    assert not answer_receipt_errors(report)
    row["final_sql"] = "SELECT 2 AS n"
    assert answer_receipt_errors(report) == ["synthetic:missing_sql_execution_receipt"]
    row["trace"][0]["sql"] = row["final_sql"]
    assert answer_receipt_errors(report) == ["synthetic:unverified_sql_data_dependency"]
    row["final_sql"] = row["trace"][0]["sql"] = "SELECT COUNT(*) AS n, 2 AS guess FROM people"
    assert answer_receipt_errors(report) == ["synthetic:unverified_sql_data_dependency"]
    row["passed"] = False
    assert not answer_receipt_errors(report)


def _rule_row(method_id: str) -> dict:
    return {
        "case_id": "synthetic",
        "passed": True,
        "answers": {"fee": 0.12},
        "answer_evidence": {
            "fee": {
                "step": 2,
                "path": ["average_fee"],
                "method_id": method_id,
                "result_hash": "same",
                "submitted_value": 0.12,
            }
        },
        "trace": [
            {"step": 1, "status": "ok", "tool": "document_search", "result_hash": "doc"},
            {"step": 2, "status": "ok", "tool": "fee_rules", "result_hash": "same"},
        ],
    }


def test_rule_tool_field_receipt_is_not_reported_as_a_relabelled_sql_method():
    from generaldataagent.evaluation.stage_seven_acceptance import answer_receipt_errors

    report = {"benchmark": "dabstep-dev", "results": [_rule_row("fee_rules")]}
    assert answer_receipt_errors(report) == []
    report["results"] = [_rule_row("sql")]
    assert answer_receipt_errors(report) == ["synthetic:receipt_method_mismatch"]


def test_field_receipt_from_an_unlisted_tool_fails_closed():
    from generaldataagent.evaluation.stage_seven_acceptance import answer_receipt_errors

    row = _rule_row("fee_rules")
    row["trace"][1]["tool"] = "unlisted_tool"
    report = {"benchmark": "dabstep-dev", "results": [row]}
    assert answer_receipt_errors(report) == ["synthetic:receipt_method_mismatch"]
