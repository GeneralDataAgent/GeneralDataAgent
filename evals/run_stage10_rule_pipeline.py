"""Run the deterministic rule pipeline over the public development questions.

What this measures, precisely: for every question in the public development
file, either

* the deterministic parser refuses it (recorded with a stable code), or
* the parser accepts it and the deterministic rule workflow computes an answer,
  which is then compared with the published answer.

It calls **no model**. It is therefore not a benchmark score, not a
supervisor-quality measurement and not comparable with ``evals/run_stage6.py``
runs. Its value is narrower and still real: it shows which questions the
deterministic path can answer at all, and whether those answers are right.

The full denominator is always reported. Refusals are never dropped, never
retried with a guess and never counted as passes.

The annex semantics reading is a required argument and is validated against the
supplied documents; its fingerprint goes into the report so a run can never be
read without knowing which reading produced it. See
``docs/validation/stage10-rule-semantics.md``.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from generaldataagent.business.rules.payments import SQLitePaymentsSource
from generaldataagent.business.rules.question import RuleQuestionError, parse_rule_question
from generaldataagent.business.rules.semantics import AnnexSemanticsDeclaration
from generaldataagent.business.rules.workflow import AnnexWorkflowServices
from generaldataagent.contracts import ActorContext
from generaldataagent.evaluation.stage_six import add_json_tables, build_csv_database, digest
from generaldataagent.orchestration.checkpoints import open_protected_checkpointer
from generaldataagent.orchestration.rule_workflow import RuleWorkflowRuntime

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "data/stage6/dabstep"


def _numeric_match(actual: str, expected: str, *, relative: float = 1e-6) -> bool:
    try:
        a, b = float(actual), float(expected)
    except (TypeError, ValueError):
        return False
    return math.isfinite(a) and math.isfinite(b) and abs(a - b) <= relative * max(1.0, abs(b))


def _id_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _compare(operation: str, delivery: dict, published: str) -> tuple[bool, dict]:
    expected = published.strip()
    if operation in {"select", "applicable"}:
        actual = [str(item) for item in delivery.get("items") or []]
        want = _id_list(expected)
        matched = sorted(actual, key=_sort_key) == sorted(want, key=_sort_key)
        return matched, {"kind": "id_list", "actual_n": len(actual), "published_n": len(want)}
    key = "average_fee" if operation == "average_fee" else "delta"
    actual = delivery.get(key)
    matched = actual is not None and _numeric_match(str(actual), expected)
    return matched, {"kind": key, "actual": actual, "published": expected}


def _sort_key(value: str) -> tuple[int, str]:
    return (0, value.zfill(8)) if value.isdigit() else (1, value)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--empty-list-is-wildcard",
        action=argparse.BooleanOptionalAction,
        required=True,
        help="The declared reading of an empty list in the annex; no default is assumed.",
    )
    parser.add_argument(
        "--semantics-excerpt",
        default=(
            "If a field is set to null it means that it applies to all possible "
            "values of that field."
        ),
    )
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("evaluation output already exists")

    documents = "\n".join(
        (DATASET / name).read_text() for name in ("manual.md", "payments-readme.md")
    )
    declaration = AnnexSemanticsDeclaration(
        empty_list_is_wildcard=args.empty_list_is_wildcard,
        source_excerpt=args.semantics_excerpt,
    ).validated_against(documents)
    merchants = tuple(
        entry["merchant"] for entry in json.loads((DATASET / "merchant_data.json").read_text())
    )
    mcc = {
        row["description"]: int(row["mcc"])
        for row in csv.DictReader((DATASET / "merchant_category_codes.csv").open())
    }
    questions = [json.loads(line) for line in (DATASET / "dev.jsonl").read_text().splitlines()]
    annex = json.loads((DATASET / "fees.json").read_text())
    actor = ActorContext(
        user_id="stage10-evaluation",
        tenant_id="evaluation",
        project_id="dabstep-rules",
        data_domains=("payments",),
    )
    report = {
        "runner_hash": digest(Path(__file__)),
        "dataset_hashes": {
            name: digest(DATASET / name)
            for name in (
                "dev.jsonl",
                "fees.json",
                "merchant_data.json",
                "merchant_category_codes.csv",
                "manual.md",
            )
        },
        "annex_semantics": declaration.summary(),
        "evaluation": "deterministic-rule-pipeline",
        "model_calls": 0,
        "public_benchmark_score": False,
        "supervisor_measured": False,
        "production_accepted": False,
        "cost_usd": None,
        "started_at": datetime.now(UTC).isoformat(),
        "results": [],
    }
    with tempfile.TemporaryDirectory() as folder:
        database = Path(folder) / "payments.sqlite"
        build_csv_database(
            {
                name: DATASET / (name + ".csv")
                for name in ("payments", "acquirer_countries", "merchant_category_codes")
            },
            database,
        )
        add_json_tables(
            {name: DATASET / (name + ".json") for name in ("fees", "merchant_data")},
            database,
        )
        services = AnnexWorkflowServices(
            annex,
            documents=documents,
            payments=SQLitePaymentsSource(
                database, allowed_relations={"merchant_data", "payments"}
            ),
        )
        with open_protected_checkpointer(Path(folder) / "state", encryption_key=b"k" * 32) as saver:
            flow = RuleWorkflowRuntime(
                services=services.services(),
                checkpointer=saver,
                source_hashes=services.source_hashes,
                workflow_version="stage10-rule-pipeline",
            )
            for question in questions:
                row = {"task_id": question["task_id"], "level": question["level"]}
                try:
                    request = parse_rule_question(
                        question["question"],
                        known_merchants=merchants,
                        semantics_excerpt=args.semantics_excerpt,
                        mcc_descriptions=mcc,
                        empty_list_is_wildcard=args.empty_list_is_wildcard,
                    )
                except RuleQuestionError as exc:
                    row.update(outcome="refused", refusal_code=exc.code, passed=False)
                    report["results"].append(row)
                    print(
                        json.dumps(
                            {"task_id": row["task_id"], "outcome": "refused", "code": exc.code}
                        ),
                        flush=True,
                    )
                    continue
                declaration.apply_to(request)
                row["operation"] = request.operation
                try:
                    _, state = flow.run(request, actor)
                except Exception as exc:
                    row.update(
                        outcome="workflow_error",
                        error=type(exc).__name__,
                        passed=False,
                    )
                    report["results"].append(row)
                    print(
                        json.dumps({"task_id": row["task_id"], "outcome": "workflow_error"}),
                        flush=True,
                    )
                    continue
                verification = state.get("verification") or {}
                matched, detail = _compare(
                    request.operation, state.get("delivery") or {}, str(question["answer"])
                )
                row.update(
                    outcome="computed",
                    verification_passed=verification.get("passed") is True,
                    comparison=detail,
                    passed=bool(verification.get("passed") is True and matched),
                )
                report["results"].append(row)
                print(
                    json.dumps(
                        {"task_id": row["task_id"], "outcome": "computed", "passed": row["passed"]}
                    ),
                    flush=True,
                )

    computed = [r for r in report["results"] if r["outcome"] == "computed"]
    refused = [r for r in report["results"] if r["outcome"] == "refused"]
    report["summary"] = {
        "n": len(report["results"]),
        "computed_n": len(computed),
        "computed_passed": sum(r["passed"] for r in computed),
        "refused_n": len(refused),
        "refused_by_code": {
            code: sum(1 for r in refused if r["refusal_code"] == code)
            for code in sorted({r["refusal_code"] for r in refused})
        },
        "workflow_error_n": sum(1 for r in report["results"] if r["outcome"] == "workflow_error"),
        # The denominator stays the full question set; a refusal is not a pass.
        "passed_over_all_questions": sum(r["passed"] for r in report["results"]),
    }
    report["completed_at"] = datetime.now(UTC).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["summary"], indent=2), flush=True)


if __name__ == "__main__":
    main()
