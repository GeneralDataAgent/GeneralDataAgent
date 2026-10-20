"""Compute repeat evidence; never manufacture repeats by copying a report."""

import argparse
import json
from pathlib import Path

from generaldataagent.evaluation.stage_six_stability import repeated_evidence


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = repeated_evidence([json.loads(p.read_text()) for p in args.report])
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["qualified"] else 1)


if __name__ == "__main__":
    main()
