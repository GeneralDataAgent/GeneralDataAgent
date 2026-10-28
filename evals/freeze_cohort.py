"""Declare an unused evaluation cohort from prior reports; running it stays separate."""

import argparse
import json
from pathlib import Path

from generaldataagent.evaluation.frozen_cohort import freeze_cohort
from generaldataagent.evaluation.stage_six import load_bird, load_dabstep, load_infi

LOADERS = {
    "infi": (Path("infi"), load_infi),
    "dabstep": (Path("dabstep"), load_dabstep),
    "bird": (Path("bird/mini_dev_sqlite.json"), load_bird),
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", choices=sorted(LOADERS), required=True)
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--seed", type=int, default=20261020)
    parser.add_argument("--data-root", type=Path, default=Path("data/stage6"))
    parser.add_argument(
        "--report",
        type=Path,
        action="append",
        default=[],
        help="A prior report for this benchmark; repeat for every run that must count as used.",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        action="append",
        default=[],
        help="Scan this tree for **/report.json and use those of this benchmark.",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    relative, loader = LOADERS[args.benchmark]
    cases = loader(args.data_root / relative)
    benchmark = cases[0].task.benchmark
    paths = list(args.report)
    for directory in args.report_dir:
        paths.extend(sorted(directory.glob("**/report.json")))
    # The runner writes a report directory named report.json; only files are reports.
    paths = [path for path in paths if path.is_file()]
    reports = [
        report
        for report in (json.loads(path.read_text()) for path in paths)
        if report.get("benchmark") == benchmark
    ]
    cohort = freeze_cohort(
        cases,
        benchmark=benchmark,
        limit=args.limit,
        seed=args.seed,
        reports=reports,
    )
    print(f"scanned {len(paths)} reports; used {len(reports)} for {benchmark}")
    args.output.write_text(json.dumps(cohort, indent=2) + "\n")
    print(
        json.dumps(
            {key: cohort[key] for key in ("benchmark", "limit", "case_ids", "cohort_hash")},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
