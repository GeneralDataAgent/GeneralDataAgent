"""Read-only progress for frozen full runs, keeping pending historical cases in the guard."""

import argparse
import json
from collections import Counter
from pathlib import Path

TRACKS = {"bird": 60, "infi": 60, "dabstep": 10}


def snapshot(root: Path, historical_root: Path) -> dict:
    output, commits, protocols = [], set(), set()
    historical_n = 0
    for track, expected in TRACKS.items():
        old = json.loads((historical_root / f"magpie-{track}-full-v10/report.json").read_text())
        protected = {row["case_id"] for row in old["results"] if row["passed"]}
        historical_n += len(protected)
        for arm in ("baseline", "candidate"):
            directory = root / f"{track}-{arm}"
            final = directory / "report.json"
            path = final if final.exists() else directory / "report.partial.json"
            entry = {
                "track": track,
                "arm": arm,
                "expected": expected,
                "status": "initializing_or_running" if directory.exists() else "not_started",
            }
            if path.exists():
                try:
                    report = json.loads(path.read_text())
                except json.JSONDecodeError:
                    entry["status"] = "snapshot_write_in_progress"
                    output.append(entry)
                    continue
                rows = report["results"]
                seen = {row["case_id"]: row for row in rows}
                if len(seen) != len(rows) or len(rows) > expected:
                    raise ValueError("duplicate or oversized progress cohort")
                attempted = sum(row.get("attempted", True) for row in rows)
                commits.add(report["git_commit"])
                protocols.add(report["protocol_hash"])
                entry.update(
                    recorded=len(rows),
                    attempted=attempted,
                    passed=sum(row["passed"] for row in rows),
                    failures=dict(Counter(row["failure"] for row in rows if not row["passed"])),
                    status="complete" if final.exists() else "running",
                    source_commit=report["git_commit"],
                )
                if final.exists() and attempted < expected:
                    entry["status"] = "complete_with_unattempted_cases"
                if arm == "candidate":
                    entry["historical_correct_guard"] = {
                        "total": len(protected),
                        "retained": sorted(
                            key for key in protected & seen.keys() if seen[key]["passed"]
                        ),
                        "regressed": sorted(
                            key for key in protected & seen.keys() if not seen[key]["passed"]
                        ),
                        "pending": sorted(protected - seen.keys()),
                        "source_snapshot_matches": report["source_manifest_hash"]
                        == old["source_manifest_hash"],
                    }
            output.append(entry)
    return {
        "runs": output,
        "source_commits": sorted(commits),
        "protocol_hashes": sorted(protocols),
        "frozen_source_consistent": len(commits) <= 1,
        "paired_protocol_consistent": len(protocols) <= 1,
        "historical_correct_n": historical_n,
        "production_accepted": False,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--historical-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = snapshot(args.root, args.historical_root)
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
