"""Aggregate exact case evidence into explicit, non-compensating release gates."""

import argparse
import json
from pathlib import Path

from generaldataagent.evaluation.stage_six_gates import release_diagnostics


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, action="append", required=True)
    parser.add_argument("--baseline", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = release_diagnostics(
        [json.loads(p.read_text()) for p in args.report],
        baselines=[json.loads(p.read_text()) for p in args.baseline],
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
