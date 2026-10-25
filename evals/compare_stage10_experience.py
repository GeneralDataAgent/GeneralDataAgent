"""Compare complete experience off/on reports without changing their protocols."""

import argparse
import json
from pathlib import Path

from generaldataagent.evaluation.experience_comparison import compare_experience_runs

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT) or output.exists():
        parser.error("output must be new and inside the implementation workspace")
    result = compare_experience_runs(
        json.loads(args.baseline.read_bytes()), json.loads(args.candidate.read_bytes())
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: result[key]
                for key in ("n", "improvements", "regressions", "experience_delivery", "blocked_by")
            }
        )
    )


if __name__ == "__main__":
    main()
