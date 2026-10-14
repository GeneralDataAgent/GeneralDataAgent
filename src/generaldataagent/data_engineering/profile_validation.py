"""Compare a generated profile with an independently curated oracle profile."""

import math
from typing import Any

from generaldataagent.contracts.data_engineering import (
    DataProfile,
    ProfileComparison,
    ProfileMismatch,
)

_COLUMN_METRICS = ("null_count", "distinct_count", "minimum", "maximum", "mean")


def compare_profiles(
    actual: DataProfile,
    expected: DataProfile,
    *,
    numeric_tolerance: float = 1e-9,
    threshold: float = 0.99,
) -> ProfileComparison:
    if numeric_tolerance < 0:
        raise ValueError("numeric_tolerance must be non-negative")
    if not 0 <= threshold <= 1:
        raise ValueError("threshold must be between zero and one")
    if actual.relation.casefold() != expected.relation.casefold():
        raise ValueError("profile comparison requires the same logical relation")

    total_checks = 1
    mismatches: list[ProfileMismatch] = []
    if actual.row_count != expected.row_count:
        mismatches.append(
            ProfileMismatch(
                metric="row_count",
                expected=expected.row_count,
                actual=actual.row_count,
            )
        )

    actual_columns = {column.name.casefold(): column for column in actual.columns}
    expected_columns = {column.name.casefold(): column for column in expected.columns}
    for key in sorted(actual_columns.keys() | expected_columns.keys()):
        actual_column = actual_columns.get(key)
        expected_column = expected_columns.get(key)
        total_checks += 1
        if actual_column is None or expected_column is None:
            column_name = (
                expected_column.name if expected_column is not None else actual_column.name
            )
            mismatches.append(
                ProfileMismatch(
                    column=column_name,
                    metric="column_present",
                    expected=expected_column is not None,
                    actual=actual_column is not None,
                )
            )
            for metric in _COLUMN_METRICS:
                total_checks += 1
                mismatches.append(
                    ProfileMismatch(
                        column=column_name,
                        metric=metric,
                        expected=(
                            getattr(expected_column, metric)
                            if expected_column is not None
                            else None
                        ),
                        actual=(
                            getattr(actual_column, metric) if actual_column is not None else None
                        ),
                    )
                )
            continue
        for metric in _COLUMN_METRICS:
            total_checks += 1
            actual_value = getattr(actual_column, metric)
            expected_value = getattr(expected_column, metric)
            if not _values_match(actual_value, expected_value, numeric_tolerance):
                mismatches.append(
                    ProfileMismatch(
                        column=expected_column.name,
                        metric=metric,
                        expected=expected_value,
                        actual=actual_value,
                    )
                )
    matched_checks = total_checks - len(mismatches)
    agreement_rate = matched_checks / total_checks
    return ProfileComparison(
        total_checks=total_checks,
        matched_checks=matched_checks,
        agreement_rate=agreement_rate,
        threshold=threshold,
        passed=agreement_rate >= threshold,
        mismatches=tuple(mismatches),
    )


def _values_match(actual: Any, expected: Any, tolerance: float) -> bool:
    if actual is None or expected is None:
        return actual is expected
    if (
        isinstance(actual, (int, float))
        and not isinstance(actual, bool)
        and isinstance(expected, (int, float))
        and not isinstance(expected, bool)
    ):
        return math.isclose(float(actual), float(expected), abs_tol=tolerance, rel_tol=tolerance)
    return actual == expected
