"""Compare complete frozen cohorts without excluding model-service failures."""

import argparse
import json
from pathlib import Path

from generaldataagent.evaluation.stage_six_comparison import compare_runs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = compare_runs(
        json.loads(args.baseline.read_text()), json.loads(args.candidate.read_text())
    )
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
