"""Deterministic task routing and cause-aware retry policy.

Two decisions are made here, both outside the model loop and both answer-blind:

* which execution lane a task belongs to (short SQL, fixed analysis, or the
  governed plan/execute/verify path), and therefore what budget it gets;
* whether a failure is the kind that retrying can fix, or the kind where
  retrying only burns the budget.

Routing reads only public task text. It never sees expected answers, reference
SQL or case identifiers, and it records the exact signals that drove its choice
so a reviewer can audit a misroute instead of guessing at it.

This module has not been validated against a paired benchmark run. Until it is,
routing is a declared policy, not a measured improvement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Lane = Literal["short", "analysis", "governed"]
Cause = Literal["parse", "compute", "evidence", "capability", "ambiguity", "resource"]

# Public markers, matched case-insensitively against the question and the
# published guidelines only. Order matters: governed outranks analysis, and
# analysis outranks short, so a mixed question takes the more capable lane.
_RULE_MARKERS = (
    "manual",
    "fee",
    "fees",
    "rate",
    "threshold",
    "merchant category",
    "card scheme",
    "interchange",
    "per transaction",
)
_ANALYSIS_MARKERS = (
    "correlation",
    "regression",
    "p-value",
    "p value",
    "t-test",
    "significance",
    "significant",
    "standard deviation",
    "median",
    "percentile",
    "distribution",
    "pearson",
)

_LANES: dict[Lane, tuple[str, int, int, int]] = {
    # lane: (tool profile, max steps, max seconds, max repairs)
    "short": ("sql-repair", 5, 90, 2),
    "analysis": ("sql-science", 7, 150, 3),
    "governed": ("sql-rules", 8, 180, 3),
}


@dataclass(frozen=True, slots=True)
class Route:
    lane: Lane
    tool_profile: str
    max_steps: int
    max_seconds: int
    max_repairs: int
    signals: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "lane": self.lane,
            "tool_profile": self.tool_profile,
            "max_steps": self.max_steps,
            "max_seconds": self.max_seconds,
            "max_repairs": self.max_repairs,
            "signals": list(self.signals),
        }


def _markers(text: str, markers: tuple[str, ...]) -> list[str]:
    lowered = text.casefold()
    return [marker for marker in markers if marker in lowered]


def route_task(question: str, instructions: str = "", *, override: str | None = None) -> Route:
    """Choose a lane from public text, or honour an explicit operator override.

    A question that matches nothing takes the short lane. That is a deliberate
    bias: the short lane has the smallest budget, so a misroute toward it shows
    up as a visible budget failure rather than as silent extra spend.
    """
    if not isinstance(question, str) or not question.strip():
        raise ValueError("routing requires the public question text")
    if not isinstance(instructions, str):
        raise ValueError("routing instructions must be text")
    public = f"{question}\n{instructions}"
    if override is not None:
        if override not in _LANES:
            raise ValueError("unknown routing lane override")
        return _build(override, ("operator_override",))
    rule_signals = _markers(public, _RULE_MARKERS)
    if rule_signals:
        return _build("governed", tuple(rule_signals))
    analysis_signals = _markers(public, _ANALYSIS_MARKERS)
    if analysis_signals:
        return _build("analysis", tuple(analysis_signals))
    return _build("short", ())


def _build(lane: Lane, signals: tuple[str, ...]) -> Route:
    profile, steps, seconds, repairs = _LANES[lane]
    return Route(
        lane=lane,
        tool_profile=profile,
        max_steps=steps,
        max_seconds=seconds,
        max_repairs=repairs,
        signals=signals,
    )


# -- cause-aware retry -------------------------------------------------------

# A failure the model can plausibly fix by looking at the feedback again.
_PARSE_CODES = frozenset(
    {
        "unknown_column",
        "unknown_table",
        "ambiguous_column",
        "sql_syntax",
        "action_schema",
        "function_arguments",
        "invalid_action",
        "invalid_output",
        "rule_context_required",
        "rule_time_scope_required",
        "rule_context_operation_required",
        "rule_rate_change_required",
        "rule_counterfactual_operation_required",
        "rule_semantics_excerpt_missing",
        "rule_semantics_null_required",
        "rule_formula_source_missing",
        "rule_merchant_not_unique",
        "rule_relation_denied",
    }
)
_COMPUTE_CODES = frozenset(
    {
        "unsupported_function",
        "empty_analysis_population",
        "semantic_contract",
        "rule_month_empty",
        "rule_zero_monthly_volume",
        "rule_counterfactual_not_applicable",
    }
)
_CAPABILITY_CODES = frozenset({"unsupported_capability", "policy_rejection"})
# A timeout or a row/cell cap is a signal to narrow the query, not evidence the
# task is impossible. Only a spent budget means retrying cannot help.
_RESOURCE_CODES = frozenset({"time_budget", "cost_budget", "repair_budget_exhausted"})
_NARROWING_CODES = frozenset({"query_timeout", "result_limit"})
_AMBIGUITY_CODES = frozenset({"multiple_applicable_rules", "ambiguous_metric"})

# Only these causes are worth another attempt. Retrying a missing capability or
# an ambiguous request just spends budget on a question the system cannot answer.
_RETRYABLE: dict[Cause, bool] = {
    "parse": True,
    "compute": True,
    "evidence": True,
    "capability": False,
    "ambiguity": False,
    "resource": False,
}


def failure_cause(code: str) -> Cause:
    """Classify a stable failure code. Unknown codes are treated as fixable.

    Defaulting to ``parse`` is deliberate: an unrecognised code is more likely a
    new error message than a missing capability, and the repair budget already
    bounds how long the model may keep trying.
    """
    if not isinstance(code, str) or not code.strip():
        raise ValueError("failure classification requires a stable code")
    if code == "missing_evidence":
        return "evidence"
    if code in _CAPABILITY_CODES:
        return "capability"
    if code in _RESOURCE_CODES:
        return "resource"
    if code in _AMBIGUITY_CODES:
        return "ambiguity"
    if code in _COMPUTE_CODES or code in _NARROWING_CODES:
        return "compute"
    return "parse"


def should_retry(code: str) -> bool:
    return _RETRYABLE[failure_cause(code)]


__all__ = [
    "Cause",
    "Lane",
    "Route",
    "failure_cause",
    "route_task",
    "should_retry",
]
