"""Authored NL development qualification, isolated from benchmark questions/labels."""

import hashlib
import json
from fractions import Fraction
from pathlib import Path

from generaldataagent.business.rules import question as question_module
from generaldataagent.business.rules.payments import SQLitePaymentsSource
from generaldataagent.business.rules.question import RuleQuestionError, parse_rule_question
from generaldataagent.business.rules.workflow import AnnexWorkflowServices
from generaldataagent.contracts import ActorContext
from generaldataagent.contracts.rule_tools import RuleAnalysisRequest
from generaldataagent.evaluation import rule_governance_qualification, rule_qualification
from generaldataagent.evaluation.rule_governance_qualification import run_governed_case
from generaldataagent.evaluation.rule_qualification import (
    _context_database,
    independent_context_expected,
    independent_expected,
)
from generaldataagent.orchestration.checkpoints import open_protected_checkpointer
from generaldataagent.orchestration.rule_workflow import RuleWorkflowRuntime


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _close(actual, expected):
    if expected is None:
        return actual is None
    return actual is not None and abs(Fraction(str(actual)) - expected) <= Fraction(1, 10**24)


def qualify_business(corpus: Path, output: Path, *, governed: bool = False) -> dict:
    """Check exact parsed requests before running workflow against raw-record oracles."""
    payload = corpus.read_bytes()
    data = json.loads(payload)
    cases = data["cases"]
    ids = [case["case_id"] for case in cases]
    if not cases or len(set(ids)) != len(ids):
        raise ValueError("qualification requires nonempty uniquely identified cases")
    for case in cases:
        if ("request" in case) == ("refusal" in case):
            raise ValueError("each case requires exactly one request or refusal expectation")
    output.mkdir(parents=True, exist_ok=False)
    (output / "corpus.json").write_bytes(payload)
    report = dict(
        suite=data["suite"],
        provenance=data["provenance"],
        corpus_sha256=hashlib.sha256(payload).hexdigest(),
        source_sha256={
            "qualification": _digest(Path(__file__)),
            "parser": _digest(Path(question_module.__file__)),
            "oracle": _digest(Path(rule_qualification.__file__)),
            "governance_qualification": _digest(Path(rule_governance_qualification.__file__)),
        },
        planned_case_ids=ids,
        n=len(cases),
        processed_n=0,
        pending_n=len(cases),
        passed=0,
        run_status="running",
        results=[],
        model_calls=0,
        execution_mode="development_gateway" if governed else "direct_workflow",
        benchmark_score=False,
        production_accepted=False,
        independent_human_review="not_run",
        unseen_evaluation="not_run",
        limitations=[
            "authored and inspected synthetic development cases, not a hidden holdout",
            "numeric oracles use raw records and Fraction but share supplied annex semantics",
            "does not exercise model delegation or production release admission",
            "development gateway only; no production source authorization or retention"
            if governed
            else "direct workflow; no tool governance checks",
        ],
    )

    def persist():
        report["processed_n"] = len(report["results"])
        report["pending_n"] = report["n"] - report["processed_n"]
        report["passed"] = sum(row["passed"] for row in report["results"])
        report["governance_checked_n"] = sum("governance" in row for row in report["results"])
        report["governance_passed_n"] = sum(
            row.get("governance", {}).get("passed", False) for row in report["results"]
        )
        partial = output / "report.partial"
        partial.write_text(json.dumps(report, indent=2) + "\n")
        partial.replace(output / "report.json")

    persist()
    actor = ActorContext(
        user_id="qualification",
        tenant_id="synthetic",
        project_id="rules",
        data_domains=("synthetic",),
    )
    database = output / "payments.sqlite"
    _context_database(database, data["merchant"], data["payments"])
    payments = SQLitePaymentsSource(database, allowed_relations={"merchant_data", "payments"})
    with open_protected_checkpointer(
        output / "checkpoints.sqlite", encryption_key=b"synthetic-rule-qualification-key!"[:32]
    ) as saver:
        for case in cases:
            row = dict(case_id=case["case_id"], passed=False)
            declaration = dict(
                semantics_excerpt=data["semantics_excerpt"],
                empty_list_is_wildcard=case.get("empty_list_is_wildcard", False),
            )
            try:
                request = parse_rule_question(
                    case["question"],
                    known_merchants=(data["merchant"]["merchant"],),
                    mcc_descriptions=data["mcc_descriptions"],
                    **declaration,
                )
                row["observed_request"] = request.model_dump()
                if "refusal" in case:
                    row["outcome"] = "unexpected_acceptance"
                    row["expected_refusal"] = case["refusal"]
                else:
                    expected = RuleAnalysisRequest(**(declaration | case["request"]))
                    row["expected_request"] = expected.model_dump()
                    if request != expected:
                        row["outcome"] = "request_mismatch"
                    else:
                        adapter = AnnexWorkflowServices(
                            data["annex"], documents=data["documents"], payments=payments
                        )
                        row["source_hashes"] = adapter.source_hashes()
                        flow = RuleWorkflowRuntime(
                            services=adapter.services(),
                            checkpointer=saver,
                            source_hashes=adapter.source_hashes,
                            workflow_version=data["suite"],
                        )
                        expected_request = expected.model_dump()
                        contextual = expected.operation in {"applicable", "counterfactual"}
                        if contextual:
                            selected, change = independent_context_expected(
                                data["merchant"], data["payments"], data["annex"], expected_request
                            )
                            if change == "not_applicable":
                                raise ValueError(
                                    "corpus requires applicable counterfactual targets"
                                )
                            mean = None
                        else:
                            selected, mean = independent_expected(data["annex"], expected_request)
                            change = None
                        row["expected_answer"] = dict(
                            items=selected,
                            average_fee=str(mean) if mean is not None else None,
                            change={key: str(value) for key, value in (change or {}).items()},
                        )
                        if governed:
                            thread, state, row["governance"] = run_governed_case(
                                flow, request, actor, output / f"case-{len(report['results']):04d}"
                            )
                        else:
                            thread, state = flow.run(request, actor)
                        answer = state.get("delivery", {})
                        row.update(thread_id=thread, status=state["status"], observed_answer=answer)
                        if expected.operation == "counterfactual":
                            matches = all(
                                _close(answer.get(key), value) for key, value in change.items()
                            )
                        else:
                            matches = answer.get("items") == selected
                            if expected.operation == "average_fee":
                                matches = matches and _close(answer.get("average_fee"), mean)
                        row.update(
                            outcome="workflow_result",
                            passed=state["status"] == "completed"
                            and matches
                            and (not governed or row["governance"]["passed"]),
                        )
            except RuleQuestionError as exc:
                row.update(
                    outcome="parser_refusal",
                    observed_refusal=exc.code,
                    expected_refusal=case.get("refusal"),
                    passed=case.get("refusal") == exc.code,
                )
            except Exception as exc:
                row.update(outcome="error", error_type=type(exc).__name__)
            report["results"].append(row)
            persist()
    report["run_status"] = "completed"
    persist()
    return report
