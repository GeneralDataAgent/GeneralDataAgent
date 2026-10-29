"""Extract a bounded manifest of development traces into a new candidate store."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from generaldataagent.business.experience_batch import capture_batch
from generaldataagent.contracts import ActorContext
from generaldataagent.experience.extraction import extraction_source_hash

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--actor", required=True, help="ActorContext JSON")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT) or output.exists():
        parser.error("output must be new and inside the implementation workspace")
    report = capture_batch(
        args.manifest,
        output,
        actor=ActorContext.model_validate_json(args.actor),
        created_at=datetime.now(UTC),
        extractor_hash=extraction_source_hash(ROOT),
    )
    print(
        json.dumps(
            {
                key: report[key]
                for key in ("run_status", "planned_items", "counts", "candidate_count")
            }
        )
    )
    if report["run_status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
