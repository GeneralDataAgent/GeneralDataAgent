"""Aggregate paired repeats by task; never manufacture a repeat from one run."""

import argparse
import json
from pathlib import Path

from generaldataagent.evaluation.frozen_cohort import (
    frozen_file_hash,
    load_frozen_cohort,
)
from generaldataagent.evaluation.repeat_comparison import compare_repeated_runs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, action="append", required=True)
    parser.add_argument("--candidate", type=Path, action="append", required=True)
    parser.add_argument(
        "--cohort-status", choices=["development", "held_out"], default="development"
    )
    parser.add_argument(
        "--cohort-file",
        type=Path,
        help="The frozen cohort file the runs claim; it is hashed and checked, not trusted",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = compare_repeated_runs(
        [json.loads(path.read_text()) for path in args.baseline],
        [json.loads(path.read_text()) for path in args.candidate],
        cohort_status=args.cohort_status,
        cohort_file=load_frozen_cohort(args.cohort_file) if args.cohort_file else None,
        cohort_file_sha256=frozen_file_hash(args.cohort_file) if args.cohort_file else None,
    )
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
