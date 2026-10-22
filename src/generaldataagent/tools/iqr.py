"""Deterministic inclusive linear quartiles and strict 1.5-IQR fences."""

import math
import re
import statistics


def public_iqr_conflicts(text: str) -> list[str]:
    """Recognize explicit incompatible conventions; absence is not semantic approval."""
    conflicts = []
    if re.search(r"quartile|quantile|percentile|\bIQR\b|interquartile", text, re.I) and re.search(
        r"\b(?:nearest|midpoint|exclusive|median[_ ]unbiased|normal[_ ]unbiased)\b", text, re.I
    ):
        conflicts.append("unsupported_quantile_convention")
    multipliers = re.findall(
        r"(\d+(?:\.\d+)?)\s*(?:times\s+(?:the\s+)?|\*\s*)"
        r"(?:IQR\b|interquartile\s+range\b)",
        text,
        re.I,
    )
    if any(float(value) != 1.5 for value in multipliers):
        conflicts.append("unsupported_fence_multiplier")
    return conflicts


def iqr_summary(values: list[float]) -> dict:
    """Use h=(n-1)*p, preserving duplicates and excluding no observations here.

    The caller chooses the finite-value population. This method does not certify that
    population, the suitability of IQR, or an unspecified public quantile convention.
    """
    if len(values) > 200_000 or any(
        type(value) not in {int, float} or not math.isfinite(value) for value in values
    ):
        raise ValueError("IQR requires at most 200000 finite numeric values")
    ordered = sorted(values)

    def quantile(p):
        position = (len(ordered) - 1) * p
        left = int(position)
        right = min(left + 1, len(ordered) - 1)
        weight = position - left
        return ordered[left] * (1 - weight) + ordered[right] * weight

    if not ordered:
        return dict.fromkeys(("q1", "q3", "iqr", "lower_fence", "upper_fence", "outlier_count")) | {
            "cleaned_statistics": {
                "count": 0,
                "mean": None,
                "sample_std": None,
                "population_std": None,
            }
        }
    q1, q3 = quantile(0.25), quantile(0.75)
    width = q3 - q1
    lower, upper = q1 - 1.5 * width, q3 + 1.5 * width
    if not all(math.isfinite(value) for value in (q1, q3, width, lower, upper)):
        raise ValueError("IQR arithmetic exceeded finite range")
    inliers = [value for value in ordered if lower <= value <= upper]
    return {
        "q1": q1,
        "q3": q3,
        "iqr": width,
        "lower_fence": lower,
        "upper_fence": upper,
        "outlier_count": sum(value < lower or value > upper for value in ordered),
        "cleaned_statistics": {
            "count": len(inliers),
            "mean": statistics.mean(inliers),
            "sample_std": statistics.stdev(inliers) if len(inliers) > 1 else None,
            "population_std": statistics.pstdev(inliers),
        },
    }
