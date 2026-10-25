"""Diagnose one declared rule semantics against published development answers.

This is a *diagnostic*, not a score. It answers one narrow question: for the
fee-ID questions in the public development set, which declared interpretation of
an empty list reproduces the published answer?

It reads published answers **only** to compare them after the deterministic
engine has already produced a result. Nothing here feeds a runtime, a prompt or
a stored experience, and the report says so explicitly.

The finding it produced on 2026-10-23 is recorded in
``docs/validation/stage10-rule-semantics.md``: the annex's list-type fields are
empty for "no constraint", which contradicts a literal "empty list matches
nothing" reading. The engine default stays fail-closed (``False``) until an
independent review accepts the wider reading; this script exists so that
decision is made on recorded evidence rather than convenience.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from generaldataagent.business.rules.engine import (
    applicable_rules,
    load_rule_set,
    match_rules,
    monthly_totals,
)
from generaldataagent.business.rules.payments import (
    SQLitePaymentsSource,
    scoped_month,
    transaction_facets,
)
from generaldataagent.contracts.rules import RuleSemantics, TransactionFacets
from generaldataagent.evaluation.stage_six import add_json_tables, build_csv_database, digest

ROOT = Path(__file__).resolve().parents[1]


def _merchant_months(source: SQLitePaymentsSource, merchant: str, year: int):
    record = source.merchant_year(merchant, year)
    for month in range(1, 13):
        rows = scoped_month(record, month)
        if not rows:
            continue
        volume, fraud, _, _ = monthly_totals(rows)
        if not volume:
            continue
        yield month, record, rows, volume, fraud


def diagnose(*, wildcard: bool, merchant: str, year: int) -> dict:
    dataset = ROOT / "data/stage6/dabstep"
    with tempfile.TemporaryDirectory() as folder:
        database = Path(folder) / "payments.sqlite"
        build_csv_database(
            {
                name: dataset / (name + ".csv")
                for name in ("payments", "acquirer_countries", "merchant_category_codes")
            },
            database,
        )
        add_json_tables(
            {name: dataset / (name + ".json") for name in ("fees", "merchant_data")}, database
        )
        annex = json.loads((dataset / "fees.json").read_text())
        rules = load_rule_set(
            annex,
            source="fees.json",
            semantics=RuleSemantics(empty_list_is_wildcard=wildcard, source="manual.md"),
        )
        source = SQLitePaymentsSource(database, allowed_relations={"merchant_data", "payments"})
        months = {}
        for month, record, rows, volume, fraud in _merchant_months(source, merchant, year):
            merchant_level = match_rules(
                rules,
                merchant=record.merchant,
                monthly_volume=float(volume),
                monthly_fraud_level=float(fraud / volume),
            )
            facets = [TransactionFacets(**facet) for facet in transaction_facets(rows)]
            report = applicable_rules(merchant_level, facets)
            months[month] = {
                "merchant_level_rules": len(merchant_level),
                "applicable_rules": len(report.matched),
                "applicable_ids": sorted(report.matched),
            }
    return {
        "empty_list_is_wildcard": wildcard,
        "merchant": merchant,
        "year": year,
        "months": months,
    }


_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def compare(report: dict, published: dict[str, tuple[str, str]]) -> dict:
    """Overlap with published fee-ID answers for the month each question names.

    Only the month named in the question is compared. Matching by answer size
    would let an unrelated month stand in for the asked one, which is exactly
    the kind of silent substitution this diagnostic must not do.
    """
    months = report["months"]
    comparisons = []
    skipped = []
    for task_id, (question, answer) in published.items():
        lowered = question.casefold()
        named = [value for name, value in _MONTHS.items() if name in lowered]
        # ``months`` is keyed by integer month, so compare in that space.
        if len(named) != 1 or named[0] not in months:
            skipped.append({"task_id": task_id, "reason": "month_not_identified"})
            continue
        month = named[0]
        expected = {value.strip() for value in answer.split(",") if value.strip()}
        actual = set(months[month]["applicable_ids"])
        comparisons.append(
            {
                "task_id": task_id,
                "month": month,
                "published_n": len(expected),
                "engine_n": len(actual),
                "overlap": len(expected & actual),
            }
        )
    return {"comparisons": comparisons, "skipped": skipped}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--merchant", default="Belles_cookbook_store")
    parser.add_argument("--year", type=int, default=2023)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("diagnostic output already exists")
    dataset = ROOT / "data/stage6/dabstep"
    dev = [json.loads(line) for line in (dataset / "dev.jsonl").read_text().splitlines()]
    published = {
        row["task_id"]: (row["question"], str(row["answer"]))
        for row in dev
        if "fee id" in row["question"].casefold()
        and args.merchant.casefold() in row["question"].casefold()
    }
    report = {
        "runner_hash": digest(Path(__file__)),
        "dataset_manifest_hash": digest(dataset / "fees.json"),
        "published_answer_usage": (
            "comparison after deterministic execution only; never read by a runtime, "
            "prompt or stored experience"
        ),
        "evaluation": "rule-semantics-diagnostic",
        "public_benchmark_score": False,
        "production_accepted": False,
        "cost_usd": None,
        "arms": {},
    }
    for wildcard in (False, True):
        arm = diagnose(wildcard=wildcard, merchant=args.merchant, year=args.year)
        arm.update(compare(arm, published))
        report["arms"][str(wildcard)] = arm
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    for wildcard, arm in report["arms"].items():
        total = sum(item["overlap"] for item in arm["comparisons"])
        print(
            json.dumps(
                {
                    "empty_list_is_wildcard": wildcard,
                    "months_with_rules": sum(
                        1 for v in arm["months"].values() if v["applicable_rules"]
                    ),
                    "published_overlap": total,
                    "comparisons": arm["comparisons"],
                    "skipped": arm["skipped"],
                }
            )
        )


if __name__ == "__main__":
    main()
