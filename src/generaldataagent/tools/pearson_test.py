"""Fixed two-sided Pearson test; no generated code or configurable inference method."""

import hashlib
import math
import re
import warnings
from decimal import Decimal

from generaldataagent.catalog.field_evidence import public_quoted_labels
from generaldataagent.contracts.independent_test import SignificanceRule
from generaldataagent.contracts.statistical_rules import PearsonDecisionRule


def public_decision(rule: PearsonDecisionRule, r: float, p: float, public_text: str) -> dict:
    """Apply a declared policy to unrounded results; provenance is not interpretation proof."""
    numbers = {
        Decimal(value) for value in re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?(?!\w|\.\d)", public_text)
    }
    if not {Decimal(str(rule.alpha)), Decimal(str(rule.abs_r_threshold))} <= numbers:
        raise ValueError("decision thresholds must occur in original public text")
    # Explicitly named alpha takes precedence over unrelated numeric literals. This
    # is a provenance guard, not a general natural-language policy interpreter.
    named_alpha = re.findall(
        r"\balpha\)?\s*(?:of|=|:)?\s*(\d+(?:\.\d+)?)(?!\w|\.\d)", public_text, re.I
    )
    if named_alpha and {Decimal(value) for value in named_alpha} != {Decimal(str(rule.alpha))}:
        raise ValueError("decision alpha conflicts with public named alpha")
    labels = {rule.significant_high_label, rule.significant_low_label, rule.nonsignificant_label}
    if not labels <= set(public_quoted_labels(public_text)):
        raise ValueError("decision labels must be quoted in original public text")
    label = (
        rule.nonsignificant_label
        if p >= rule.alpha
        else rule.significant_high_label
        if abs(r) >= rule.abs_r_threshold
        else rule.significant_low_label
    )
    return {
        "public_rule_label": label,
        "decision_rule": rule.model_dump(),
        "decision_source_sha256": hashlib.sha256(public_text.encode()).hexdigest(),
        "policy_interpretation_verified": False,
        "decision_scope": "public policy label; not a scientific test of linearity or nonlinearity",
    }


def pearson_test(
    rows: list,
    *,
    rule: PearsonDecisionRule | None = None,
    significance_rule: SignificanceRule | None = None,
    public_text: str = "",
) -> dict:
    import scipy
    from scipy.stats import ConstantInputWarning, NearConstantInputWarning, pearsonr

    if rule is not None and significance_rule is not None:
        raise ValueError("choose one public Pearson decision policy")
    if len(rows) < 2:
        raise ValueError("Pearson test requires at least two finite pairs")
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConstantInputWarning)
        warnings.simplefilter("error", NearConstantInputWarning)
        try:
            result = pearsonr(
                [row[0] for row in rows], [row[1] for row in rows], alternative="two-sided"
            )
        except (ConstantInputWarning, NearConstantInputWarning) as exc:
            raise ValueError("constant or numerically unstable Pearson input") from exc
    r, p = float(result.statistic), float(result.pvalue)
    if not math.isfinite(r) or not math.isfinite(p):
        raise ValueError("undefined Pearson test result")
    output = {
        "correlation": r,
        "p_value": p,
        "alternative": "two-sided",
        "test_method": (
            "scipy.stats.pearsonr two-observation exact two-point null; two-sided p=1"
            if len(rows) == 2
            else "scipy.stats.pearsonr default exact beta null distribution"
        ),
        "scipy_version": scipy.__version__,
        "assumptions": "independent normally distributed observations under zero-correlation null",
        "assumptions_verified": False,
    }
    if rule is not None:
        output.update(public_decision(rule, r, p, public_text))
    if significance_rule is not None:
        output.update(public_significance(significance_rule, p, public_text))
    return output


def public_significance(rule: SignificanceRule, p: float, public_text: str) -> dict:
    """A sourced binary p-value policy; no correlation-strength threshold is invented."""
    numbers = {
        Decimal(value) for value in re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?(?!\w|\.\d)", public_text)
    }
    if Decimal(str(rule.alpha)) not in numbers:
        raise ValueError("significance alpha must occur in original public text")
    named = re.findall(
        r"\b(?:alpha\)?|significance\s+level)\s*(?:of|=|:)?\s*"
        r"(\d+(?:\.\d+)?)(?!\w|\.\d)",
        public_text,
        re.I,
    )
    if named and {Decimal(value) for value in named} != {Decimal(str(rule.alpha))}:
        raise ValueError("significance rule conflicts with the public named level")
    if not {rule.significant_label, rule.nonsignificant_label} <= set(
        public_quoted_labels(public_text)
    ):
        raise ValueError("significance labels must be quoted in original public text")
    return {
        "public_rule_label": rule.significant_label
        if p < rule.alpha
        else rule.nonsignificant_label,
        "significance_rule": rule.model_dump(),
        "decision_source_sha256": hashlib.sha256(public_text.encode()).hexdigest(),
        "policy_interpretation_verified": False,
        "decision_scope": "public two-sided p-value policy; assumptions remain unverified",
    }
