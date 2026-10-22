"""Fresh same-protocol Stage 7 control vs Stage 8 candidate, no token caps by default."""

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from generaldataagent.evaluation.stage_eight_review import review_stage8

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--tracks",
        nargs="+",
        choices=["infi", "bird", "dabstep"],
        default=["infi", "bird", "dabstep"],
    )
    parser.add_argument("--workers", type=int, choices=[1, 2], default=1)
    parser.add_argument("--model", default="workbuddy/deepseek-v4.1-flash")
    parser.add_argument("--historical-root", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    if len(set(args.tracks)) != len(args.tracks):
        parser.error("tracks must be unique")
    status_path = args.output / "driver-status.json"
    metadata = {
        "status": "running",
        "protocol": "stage8-v1",
        "token_caps": None,
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "production_accepted": False,
        "tracks": args.tracks,
    }
    status_path.write_text(json.dumps(metadata, indent=2) + "\n")
    environment = {**os.environ, "LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false"}

    def arm(name):
        for track in args.tracks:
            command = [
                sys.executable,
                str(ROOT / "evals/run_stage6.py"),
                "--benchmark",
                track,
                "--limit",
                "10" if track == "dabstep" else "60",
                "--seed",
                "20261020",
                "--workers",
                str(args.workers),
                "--app-type",
                "magpie",
                "--model",
                args.model,
                "--transport",
                "openai-chat",
                "--csv-importer",
                "identifier-v2",
                "--evaluation-protocol",
                "stage8-v1",
                "--request-interval-seconds",
                "3.5",
                "--tool-profile",
                "sql-science" if name == "candidate" else "sql-semantic",
                "--output",
                str((args.output / f"{track}-{name}").resolve()),
            ]
            if track == "bird":
                command += ["--bird-databases", str(ROOT / "data/stage6/bird/databases")]
            with (args.output / f"{track}-{name}.log").open("w") as log:
                subprocess.run(
                    command,
                    cwd=ROOT,
                    env=environment,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=True,
                )
            report = json.loads((args.output / f"{track}-{name}/report.json").read_text())
            print(
                json.dumps(
                    {
                        "arm": name,
                        "track": track,
                        "status": report["run_status"],
                        "passed": report["summary"]["passed"],
                    }
                ),
                flush=True,
            )
            if report["run_status"] != "completed":
                raise RuntimeError("paired arm did not complete: " + track + "-" + name)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(arm, ["baseline", "candidate"]))
        comparisons = {}
        for track in args.tracks:
            historical = (
                (args.historical_root / f"magpie-{track}-full-v10/report.json")
                if args.historical_root
                else None
            )
            comparisons[track] = review_stage8(
                json.loads((args.output / f"{track}-baseline/report.json").read_text()),
                json.loads((args.output / f"{track}-candidate/report.json").read_text()),
                json.loads(historical.read_text()) if historical else None,
            )
        (args.output / "comparison.json").write_text(json.dumps(comparisons, indent=2) + "\n")
        metadata["status"] = "completed"
    except Exception as error:
        metadata.update(status="failed", error_type=type(error).__name__)
        raise
    finally:
        status_path.write_text(json.dumps(metadata, indent=2) + "\n")


if __name__ == "__main__":
    main()
