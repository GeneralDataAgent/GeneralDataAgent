"""Offline Stage 7 qualification: full pairs and the frozen historical-correct guard."""

from generaldataagent.evaluation.stage_seven_review import review_comparison
from generaldataagent.evaluation.stage_six_gates import REQUIRED_TRACKS, validate_report

TARGETS = {"bird-mini-dev-sqlite": 36, "infiagent-dabench": 32, "dabstep-dev": 1}


def historical_protection(historical: dict, candidate: dict) -> dict:
    """A regression guard across protocols, explicitly not paired causal attribution."""
    validate_report(historical)
    validate_report(candidate)
    for key in ("benchmark", "cohort_hash", "source_manifest_hash"):
        if historical.get(key) != candidate.get(key):
            raise ValueError("historical guard has a different cohort or source snapshot")
    before = {row["case_id"]: row for row in historical["results"]}
    after = {row["case_id"]: row for row in candidate["results"]}
    if before.keys() != after.keys():
        raise ValueError("historical guard requires the complete same task set")
    for key in before:
        if not before[key].get("database_hash") or before[key]["database_hash"] != after[key].get(
            "database_hash"
        ):
            raise ValueError("historical guard database snapshot differs or is missing")
    protected = sorted(key for key in before if before[key]["passed"])
    regressions = [key for key in protected if not after[key]["passed"]]
    return {
        "n": len(protected),
        "retained": [key for key in protected if key not in regressions],
        "regressions": regressions,
        "passed": not regressions,
        "protocols_equal": historical.get("protocol_hash") == candidate.get("protocol_hash"),
        "causal_attribution": False,
    }


def answer_receipt_errors(report: dict, *, read_bytes=None) -> list[str]:
    from generaldataagent.evaluation.receipts import validate_evaluation_receipts

    errors = []
    for row in report["results"]:
        try:
            if ("execution_receipts" in row or "receipt_context" in row) and read_bytes is None:
                raise ValueError("evaluation_receipt_readback_required")
            validate_evaluation_receipts(row, read_bytes=read_bytes)
        except (ValueError, TypeError, KeyError, AttributeError, IndexError, OSError):
            errors.append(row["case_id"] + ":invalid_execution_receipt")
    if report["benchmark"] == "bird-mini-dev-sqlite":
        import re

        from sqlglot import parse_one
        from sqlglot.errors import SqlglotError

        from generaldataagent.tools.sql_guard import SqlGuard
        from generaldataagent.tools.sql_semantics import sql_data_dependencies

        for row in report["results"]:
            if not row["passed"]:
                continue
            sql = row.get("final_sql")
            if not isinstance(sql, str) or not any(
                event.get("status") == "ok"
                and event.get("tool") == "sql"
                and event.get("sql") == sql
                and isinstance(event.get("result_hash"), str)
                and re.fullmatch(r"[0-9a-f]{64}", event.get("result_hash", ""))
                for event in row.get("trace", [])
            ):
                errors.append(row["case_id"] + ":missing_sql_execution_receipt")
                continue
            try:
                relations = SqlGuard(dialect="sqlite").inspect(sql).relations
                size = len(parse_one(sql, read="sqlite").selects)
                dependencies = sql_data_dependencies(
                    sql,
                    [str(i) for i in range(size)],
                    allowed_relations=set(relations),
                )
                verified = bool(dependencies) and all(dependencies)
            except (SqlglotError, ValueError, TypeError):
                verified = False
            if not verified:
                errors.append(row["case_id"] + ":unverified_sql_data_dependency")
        return sorted(set(errors))
    for row in report["results"]:
        if not row["passed"]:
            continue
        answers, receipts = row.get("answers", {}), row.get("answer_evidence", {})
        trace = {
            event["step"]: event for event in row.get("trace", []) if event.get("status") == "ok"
        }
        if not answers or set(receipts) != set(answers):
            errors.append(row["case_id"] + ":missing_field_receipt")
            continue
        for key, receipt in receipts.items():
            event = trace.get(receipt.get("step"), {})
            tool = event.get("tool")
            expected_method = (
                event.get("action", {}).get("analysis", {}).get("operation")
                if tool == "fixed_analysis"
                else "sql"
                if tool in {"sql", "sql_crosscheck"}
                else "complete_collection"
                if tool == "complete_collection"
                else None
            )
            if expected_method is None or receipt.get("method_id") != expected_method:
                errors.append(row["case_id"] + ":receipt_method_mismatch")
            if tool in {"sql", "sql_crosscheck", "complete_collection"}:
                from sqlglot import parse_one

                from generaldataagent.tools.sql_guard import SqlGuard
                from generaldataagent.tools.sql_semantics import sql_data_dependencies

                action = event.get("action", {})
                sql = (
                    event.get("sql")
                    or action.get("crosscheck", {}).get("sql")
                    or action.get("collect", {}).get("sql")
                )
                path = receipt.get("path", [])
                try:
                    relations = SqlGuard(dialect="sqlite").inspect(sql or "").relations
                    size = len(parse_one(sql, read="sqlite").selects)
                    dependencies = sql_data_dependencies(
                        sql, [str(i) for i in range(size)], allowed_relations=set(relations)
                    )
                    index = (
                        0
                        if tool == "complete_collection" and path == ["items"]
                        else (path[2] if len(path) == 3 and path[0] == "rows" else None)
                    )
                    verified = (
                        type(index) is int
                        and 0 <= index < len(dependencies)
                        and dependencies[index]
                    )
                except (ValueError, TypeError):
                    verified = False
                if not verified:
                    errors.append(row["case_id"] + ":unverified_sql_data_dependency")
            if (
                not receipt.get("method_id")
                or not receipt.get("result_hash")
                or event.get("result_hash") != receipt["result_hash"]
                or receipt.get("submitted_value") != answers[key]
            ):
                errors.append(row["case_id"] + ":invalid_field_receipt")
    return sorted(set(errors))


def qualify_stage7(
    baselines: list[dict], candidates: list[dict], historical: list[dict], *, read_bytes=None
) -> dict:
    def index(reports):
        output = {report["benchmark"]: report for report in reports}
        if len(output) != len(reports) or set(output) != set(REQUIRED_TRACKS):
            raise ValueError("qualification needs all three tracks exactly once")
        return output

    before, after, history = index(baselines), index(candidates), index(historical)
    tracks, blockers = {}, []
    for name, expected in REQUIRED_TRACKS.items():
        paired = review_comparison(before[name], after[name], [])
        protection = historical_protection(history[name], after[name])
        errors = answer_receipt_errors(after[name], read_bytes=read_bytes)
        comparison = paired["strict_comparison"]
        reasons = []
        if comparison["n"] != expected:
            reasons.append("incomplete_full_cohort")
        if comparison["candidate_passed"] < TARGETS[name]:
            reasons.append("stage7_development_target_not_met")
        if comparison["regressions"]:
            reasons.append("fresh_baseline_regressions")
        if protection["regressions"]:
            reasons.append("historical_correct_regressions")
        if not comparison["attribution_valid"]:
            reasons.append("service_confounding")
        if errors:
            reasons.append("invalid_answer_evidence")
        tracks[name] = {
            "paired": paired,
            "historical_correct_guard": protection,
            "receipt_errors": errors,
            "blocked_by": reasons,
        }
        blockers.extend(name + ":" + reason for reason in reasons)
    return {
        "validator_version": "stage7-full-with-branch-lineage-20261021",
        "stage7_development_qualified": not blockers,
        "tracks": tracks,
        "historical_correct_n": sum(t["historical_correct_guard"]["n"] for t in tracks.values()),
        "blocked_by": blockers,
        "strict_score_changed": False,
        "production_accepted": False,
        "remaining_promotion_gates": [
            "unseen_tasks",
            "three_paired_repetitions",
            "independent_review",
            "production_shadow",
        ],
    }
