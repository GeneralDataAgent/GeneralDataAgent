"""Run the independent synthetic rule capability suite without model calls."""

import argparse
import json
from pathlib import Path

from generaldataagent.evaluation.rule_qualification import qualify_context, qualify_rules


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=91307)
    parser.add_argument("--count", type=int, default=48)
    parser.add_argument("--context-count", type=int, default=24)
    parser.add_argument("--suite", choices=("facets", "context", "both"), default="both")
    args = parser.parse_args()
    reports = []
    if args.suite in {"facets", "both"}:
        reports.append(qualify_rules(args.output / "facets", seed=args.seed, count=args.count))
    if args.suite in {"context", "both"}:
        reports.append(
            qualify_context(args.output / "context", seed=args.seed, count=args.context_count)
        )
    for report in reports:
        print(json.dumps({key: value for key, value in report.items() if key != "results"}))
    passed = all(report["passed"] == report["n"] for report in reports)
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
