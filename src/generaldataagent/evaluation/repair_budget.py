"""Bounded repair accounting for evaluation agents.

The evaluator already tells the model what went wrong and how to fix it. What
it did not have is a ceiling: a model could spend its whole step budget on the
same failing call. This module counts repair opportunities by cause and stops
the loop deterministically, outside the model, when the budget is spent.

It is deliberately answer-blind: it never sees task text, expected answers or
model output, only stable failure codes and tool names.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from generaldataagent.evaluation.routing import failure_cause, should_retry

DEFAULT_MAX_REPAIRS = 3
DEFAULT_MAX_IDENTICAL = 2


@dataclass(frozen=True, slots=True)
class RepairDecision:
    """Outcome of one repair request."""

    allowed: bool
    reason: str
    attempt: int
    signature: str


@dataclass
class RepairBudget:
    """Count repair opportunities and refuse beyond a fixed ceiling.

    ``max_repairs`` bounds distinct repair attempts in total; ``max_identical``
    bounds repeats of one failure signature. Both are enforced here rather than
    by prompt wording, so the model cannot negotiate them.
    """

    max_repairs: int = DEFAULT_MAX_REPAIRS
    max_identical: int = DEFAULT_MAX_IDENTICAL
    attempted: int = 0
    by_code: dict[str, int] = field(default_factory=dict)
    by_signature: dict[str, int] = field(default_factory=dict)
    refused: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if type(self.max_repairs) is not int or self.max_repairs < 0:
            raise ValueError("repair budget must be a non-negative integer")
        if type(self.max_identical) is not int or self.max_identical < 1:
            raise ValueError("identical repair ceiling must be a positive integer")

    def request(self, code: str, *, tool: str = "") -> RepairDecision:
        """Ask for one repair opportunity; refusal is final for that signature."""
        if not isinstance(code, str) or not code.strip():
            raise ValueError("repair decisions require a stable failure code")
        signature = f"{code}::{tool or '-'}"
        cause = failure_cause(code)
        if not should_retry(code):
            # A missing capability or an ambiguous request is not a repair
            # opportunity; retrying it only spends the remaining budget.
            self.refused.append(signature)
            reason = f"repair_not_applicable_{cause}"
            return RepairDecision(False, reason, self.attempted, signature)
        if self.attempted >= self.max_repairs:
            self.refused.append(signature)
            return RepairDecision(False, "repair_budget_exhausted", self.attempted, signature)
        if self.by_signature.get(signature, 0) >= self.max_identical:
            self.refused.append(signature)
            return RepairDecision(False, "repair_repeated_identical", self.attempted, signature)
        self.attempted += 1
        self.by_code[code] = self.by_code.get(code, 0) + 1
        self.by_signature[signature] = self.by_signature.get(signature, 0) + 1
        return RepairDecision(True, "repair_allowed", self.attempted, signature)

    def report(self) -> dict:
        """Bounded telemetry; exhaustion means a retry ceiling refused repair.

        Nonrepairable causes also stop the agent, but do not spend this budget.
        """
        return {
            "attempted": self.attempted,
            "max_repairs": self.max_repairs,
            "max_identical": self.max_identical,
            "by_code": dict(sorted(self.by_code.items())),
            "refused": list(self.refused),
            "exhausted": any(
                should_retry(signature.split("::", 1)[0]) for signature in self.refused
            ),
        }
