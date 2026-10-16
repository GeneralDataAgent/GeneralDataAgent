"""Run the frozen Stage 3 data-science workbench gate."""

import argparse
import tempfile
from pathlib import Path

from generaldataagent.evaluation.stage_three import run_stage_three_evaluation


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path(__file__).parent / "datasets" / "stage3.jsonl",
    )
    parser.add_argument("--artifact-root", type=Path)
    args = parser.parse_args()
    if args.artifact_root is not None:
        report = run_stage_three_evaluation(args.dataset, args.artifact_root)
        print(report.model_dump_json(indent=2))
    else:
        with tempfile.TemporaryDirectory(prefix="gda-stage3-eval-") as directory:
            report = run_stage_three_evaluation(args.dataset, Path(directory))
            print(report.model_dump_json(indent=2))
    if report.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
