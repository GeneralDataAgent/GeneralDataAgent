"""Run development evidence outage recovery qualification without a model."""

import argparse
import json
from pathlib import Path

from generaldataagent.evaluation.rule_recovery_qualification import qualify_recovery

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fault", choices=("audit", "write"), default="audit")
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT):
        raise ValueError("output must remain inside the implementation workspace")
    report = qualify_recovery(
        ROOT / "evals/datasets/stage9-rule-business.json", output, fault=args.fault
    )
    print(json.dumps({key: value for key, value in report.items() if key != "results"}))
    raise SystemExit(0 if report["passed"] == report["n"] else 1)


if __name__ == "__main__":
    main()
