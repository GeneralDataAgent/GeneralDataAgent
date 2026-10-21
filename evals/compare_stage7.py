"""Compare fresh same-protocol runs, preserving strict scores and disputed labels separately."""

import argparse
import json
from pathlib import Path

from generaldataagent.evaluation.stage_seven_review import review_comparison


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--reviews", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output exists; choose a fresh path")
    result = review_comparison(
        json.loads(args.baseline.read_text()),
        json.loads(args.candidate.read_text()),
        json.loads(args.reviews.read_text()) if args.reviews else [],
    )
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
