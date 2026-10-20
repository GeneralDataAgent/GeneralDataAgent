"""Offline evaluation-only review ledger. Never imported by the candidate runtime."""

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field

from generaldataagent.evaluation.stage_six_comparison import compare_runs


class LabelReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    benchmark: str
    case_id: str
    issue: str = Field(min_length=1, max_length=2000)
    evidence_refs: list[str] = Field(min_length=1, max_length=20)
    status: str = "pending_independent_adjudication"


def review_comparison(baseline: dict, candidate: dict, reviews: list[dict]) -> dict:
    """Attach disputes beside the unmodified strict comparison, never award extra credit."""
    if baseline.get("protocol", {}).get("version") != "stage7-v1":
        raise ValueError("fresh stage7-v1 baseline required")
    if candidate.get("protocol", {}).get("version") != "stage7-v1":
        raise ValueError("fresh stage7-v1 candidate required")
    if baseline.get("model_route") != candidate.get("model_route"):
        raise ValueError("model route differs between paired arms")
    comparison = compare_runs(baseline, candidate)
    before = {row["case_id"]: row for row in baseline["results"]}
    after = {row["case_id"]: row for row in candidate["results"]}
    parsed = [LabelReview.model_validate(row) for row in reviews]
    keys = [(row.benchmark, row.case_id) for row in parsed]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate label review")
    for row in parsed:
        if row.benchmark != baseline["benchmark"] or row.case_id not in before:
            raise ValueError("label review outside paired cohort")
    protected = sorted(key for key in before if before[key]["passed"])
    return {
        "strict_comparison": comparison,
        "baseline_correct_protection": {
            "n": len(protected),
            "retained": [key for key in protected if after[key]["passed"]],
            "regressed": [key for key in protected if not after[key]["passed"]],
        },
        "label_reviews": [row.model_dump() for row in parsed],
        "review_digest": hashlib.sha256(json.dumps(reviews, sort_keys=True).encode()).hexdigest(),
        "strict_score_changed": False,
        "denominator_changed": False,
        "production_accepted": False,
        "limits": "development-set comparison; unseen tasks and repeated runs remain required",
    }
