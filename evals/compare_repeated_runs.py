"""Aggregate paired repeats by task; never manufacture a repeat from one run."""

import argparse
import json
from pathlib import Path

from generaldataagent.evaluation.repeat_comparison import compare_repeated_runs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, action="append", required=True)
    parser.add_argument("--candidate", type=Path, action="append", required=True)
    parser.add_argument(
        "--cohort-status", choices=["development", "held_out"], default="development"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = compare_repeated_runs(
        [json.loads(path.read_text()) for path in args.baseline],
        [json.loads(path.read_text()) for path in args.candidate],
        cohort_status=args.cohort_status,
    )
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
