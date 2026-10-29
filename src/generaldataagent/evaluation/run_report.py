"""Reusable, validated snapshots of JSON run evidence; no acceptance or authenticity claim."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field

from generaldataagent.evaluation.stage_six import summarize


@dataclass(frozen=True, slots=True, init=False)
class ValidatedRunReport:
    """Validate one detached JSON value and retain only its immutable canonical bytes.

    Views are disposable copies. A caller can change the original report or a view
    without changing subsequent diagnostics or the report hash. Constructing this
    value checks case evidence and its summary, not provenance authenticity,
    repeat independence, treatment comparability, or production acceptance.
    """

    _json: bytes = field(repr=False)
    _summary_json: bytes = field(repr=False)

    def __init__(self, report: dict):
        if not isinstance(report, dict):
            raise ValueError("run report must be a JSON object")
        try:
            encoded = json.dumps(report, sort_keys=True, allow_nan=False).encode()
            snapshot = json.loads(encoded)
        except (TypeError, ValueError) as exc:
            raise ValueError("run report must contain finite JSON evidence") from exc
        summary = _validate_snapshot(snapshot)
        object.__setattr__(self, "_json", encoded)
        object.__setattr__(self, "_summary_json", json.dumps(summary, sort_keys=True).encode())

    @classmethod
    def from_report(cls, report: dict | ValidatedRunReport) -> ValidatedRunReport:
        """Reuse an already validated snapshot instead of recomputing its summary."""
        return report if isinstance(report, cls) else cls(report)

    def to_dict(self) -> dict:
        """Return a detached JSON view, including unknown and historical fields."""
        return json.loads(self._json)

    @property
    def summary(self) -> dict:
        return json.loads(self._summary_json)

    @property
    def report_hash(self) -> str:
        """The existing sorted-JSON hash; integrity does not authenticate evidence."""
        return hashlib.sha256(self._json).hexdigest()


RunReportInput = dict | ValidatedRunReport


def _validate_snapshot(report: dict) -> dict:
    """Recompute statistics; reject duplicates, nonfinite telemetry and missing provenance."""
    for key in ("cohort_hash", "protocol_hash", "source_manifest_hash", "runner_hash", "model"):
        if not report.get(key):
            raise ValueError(f"missing provenance: {key}")
    results = report["results"]
    for row in results:
        if not isinstance(row["passed"], bool):
            raise ValueError("passed must be a boolean")
        if (
            type(row["elapsed_seconds"]) not in {int, float}
            or not math.isfinite(row["elapsed_seconds"])
            or row["elapsed_seconds"] < 0
        ):
            raise ValueError("invalid latency")
        if type(row["tokens"]) is not int or row["tokens"] < 0:
            raise ValueError("invalid token telemetry")
        if (
            not isinstance(row["case_id"], str)
            or not row["case_id"]
            or type(row["sub_hits"]) is not int
            or type(row["sub_total"]) is not int
            or not 0 <= row["sub_hits"] <= row["sub_total"]
        ):
            raise ValueError("invalid case or subquestion evidence")
        if row["passed"] and (row["sub_total"] == 0 or row["sub_hits"] != row["sub_total"]):
            raise ValueError("successful task has incomplete subquestion evidence")
        cost = row.get("cost_usd")
        if cost is not None and (
            type(cost) not in {int, float} or not math.isfinite(cost) or cost < 0
        ):
            raise ValueError("invalid cost telemetry")
        if row["passed"] and (row["failure"] or row.get("severe_incident")):
            raise ValueError("contradictory success")
    expected = summarize(results)
    if expected != report["summary"]:
        raise ValueError("summary does not match case-level evidence")
    return expected
