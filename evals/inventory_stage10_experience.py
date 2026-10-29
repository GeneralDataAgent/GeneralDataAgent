"""Prepare a scoped experience review inventory; never review or consolidate stored rows."""

import argparse
import json
from pathlib import Path

from generaldataagent.business.experience import ExperienceStore
from generaldataagent.contracts import ActorContext

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--actor", required=True, help="ActorContext JSON defining the exact scope")
    parser.add_argument("--max-records", type=int, default=1000)
    parser.add_argument("--output", type=Path, required=True, help="New scoped JSON report path")
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT) or output.exists():
        parser.error("output must be new and inside the implementation workspace")
    store = ExperienceStore(args.store, clock=lambda: None, read_only=True)
    report = store.review_inventory(
        ActorContext.model_validate_json(args.actor), max_records=args.max_records
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as stream:
        stream.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "records",
                    "distinct_lessons",
                    "repeated_lesson_records",
                    "inventory_hash",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
