"""Describe a declared Skill off/on pair without rewriting report evidence."""

import argparse
import json
from pathlib import Path

from generaldataagent.evaluation.skill_comparison import compare_skill_runs
from generaldataagent.evaluation.skill_treatment import MAX_SNAPSHOT_BYTES

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--treatment-snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT) or output.exists():
        parser.error("output must be new and inside the implementation workspace")
    with args.treatment_snapshot.open("rb") as stream:
        snapshot = stream.read(MAX_SNAPSHOT_BYTES + 1)
    if len(snapshot) > MAX_SNAPSHOT_BYTES:
        parser.error("Skill snapshot exceeds byte limit")
    result = compare_skill_runs(
        json.loads(args.baseline.read_bytes()),
        json.loads(args.candidate.read_bytes()),
        json.loads(snapshot),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: result[key]
                for key in ("n", "improvements", "regressions", "skill_delivery", "blocked_by")
            }
        )
    )


if __name__ == "__main__":
    main()
