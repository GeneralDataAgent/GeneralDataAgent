"""Extract development candidates into a separate, unreviewed store."""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from generaldataagent.business.experience import ExperienceStore
from generaldataagent.contracts import ActorContext
from generaldataagent.evaluation.experience_extraction import extract_rule_experience
from generaldataagent.evaluation.stage_six import digest

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--trace", type=Path, required=True, help="Trace array only, not a score report"
    )
    parser.add_argument("--documents", type=Path, required=True)
    parser.add_argument(
        "--actor", required=True, help="ActorContext JSON; owner is the acting user"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT) or output.exists():
        parser.error("output must be a new directory inside the implementation workspace")
    actor = ActorContext.model_validate(json.loads(args.actor))
    trace_bytes = args.trace.read_bytes()
    document_bytes = args.documents.read_bytes()
    trace = json.loads(trace_bytes.decode("utf-8"))
    if not isinstance(trace, list) or not all(isinstance(event, dict) for event in trace):
        parser.error("input must be a trace array")
    candidates = extract_rule_experience(
        trace,
        documents=document_bytes.decode("utf-8"),
        owner=actor.user_id,
        created_at=datetime.now(UTC),
    )
    output.mkdir(parents=True)
    store = ExperienceStore(output / "candidates.sqlite", clock=lambda: datetime.now(UTC))
    for candidate in candidates:
        store.capture(candidate.record, actor)
    report = {
        "trace_hash": hashlib.sha256(trace_bytes).hexdigest(),
        "documents_hash": hashlib.sha256(document_bytes).hexdigest(),
        "extractor_hash": digest(ROOT / "src/generaldataagent/evaluation/experience_extraction.py"),
        "scope_hash": ExperienceStore._scope(actor),
        "candidate_count": len(candidates),
        "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
        "independent_review": "not_run",
        "answer_correctness": "not_evaluated",
        "production_accepted": False,
        "model_calls": 0,
    }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"candidate_count": len(candidates), "independent_review": "not_run"}))


if __name__ == "__main__":
    main()
