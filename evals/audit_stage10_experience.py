"""Read-only audit of an experience store; never repairs or rewrites evidence."""

import argparse
import json
from pathlib import Path

from generaldataagent.business.experience import ExperienceStore

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New JSON report path")
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT) or output.exists():
        parser.error("output must be new and inside the implementation workspace")
    store = ExperienceStore(args.store, clock=lambda: None, read_only=True)
    report = store.audit()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as stream:
        stream.write(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "records": report["records"],
                "consistent": report["consistent"],
                "findings": len(report["findings"]),
            }
        )
    )
    if not report["consistent"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
