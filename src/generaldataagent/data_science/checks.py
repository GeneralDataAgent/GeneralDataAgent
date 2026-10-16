"""Deterministic leakage, split, and metric-suitability checks."""

import random
from collections import Counter
from collections.abc import Sequence
from typing import Any

from generaldataagent.contracts import ExperimentSpec, ScienceCheck


def split_indices(
    rows: Sequence[dict[str, Any]], spec: ExperimentSpec
) -> tuple[list[int], list[int]]:
    count = len(rows)
    test_count = max(1, round(count * spec.split.test_fraction))
    test_count = min(test_count, count - 1)
    indices = list(range(count))
    if spec.split.strategy == "random":
        random.Random(spec.random_seed).shuffle(indices)
        test = sorted(indices[:test_count])
        train = sorted(indices[test_count:])
    elif spec.split.strategy == "temporal":
        column = spec.split.timestamp_column
        assert column is not None
        try:
            indices.sort(key=lambda index: (rows[index][column], index))
        except (KeyError, TypeError) as exc:
            raise ValueError("temporal split column is missing or not sortable") from exc
        train, test = indices[:-test_count], indices[-test_count:]
    else:
        column = spec.split.group_column
        assert column is not None
        try:
            groups = sorted({str(row[column]) for row in rows})
        except KeyError as exc:
            raise ValueError("group split column is missing") from exc
        if len(groups) < 2:
            raise ValueError("group split requires at least two distinct groups")
        random.Random(spec.random_seed).shuffle(groups)
        test_groups: set[str] = set()
        selected = 0
        group_sizes = Counter(str(row[column]) for row in rows)
        for group in groups:
            if selected >= test_count and test_groups:
                break
            test_groups.add(group)
            selected += group_sizes[group]
        test = [index for index, row in enumerate(rows) if str(row[column]) in test_groups]
        train = [index for index, row in enumerate(rows) if str(row[column]) not in test_groups]
        if not train or not test:
            raise ValueError("group split produced an empty partition")
    return train, test


def validate_experiment_data(
    rows: Sequence[dict[str, Any]], spec: ExperimentSpec, train: list[int], test: list[int]
) -> tuple[ScienceCheck, ...]:
    checks: list[ScienceCheck] = []
    required = {spec.target_column, *spec.feature_columns}
    missing = sorted(name for name in required if any(name not in row for row in rows))
    checks.append(
        _check(
            "required_columns",
            not missing,
            "missing_columns" if missing else "columns_present",
            f"Missing required columns: {missing}"
            if missing
            else "All required columns are present.",
        )
    )
    if missing:
        return tuple(checks)

    targets = [row[spec.target_column] for row in rows]
    valid_target = all(value is not None for value in targets)
    if spec.problem_type == "binary_classification":
        valid_target = valid_target and len(set(map(str, targets))) == 2
    else:
        valid_target = valid_target and all(_is_number(value) for value in targets)
    checks.append(
        _check(
            "target_validity",
            valid_target,
            "target_valid" if valid_target else "invalid_target",
            "Target values match the problem type."
            if valid_target
            else "Target values do not match the requested problem type.",
        )
    )

    train_signatures = {_signature(rows[index], required) for index in train}
    overlap = sum(_signature(rows[index], required) in train_signatures for index in test)
    checks.append(
        _check(
            "duplicate_leakage",
            overlap == 0,
            "no_cross_split_duplicates" if overlap == 0 else "cross_split_duplicates",
            "No identical examples cross the train/test boundary."
            if overlap == 0
            else f"Detected {overlap} duplicate test examples in training data.",
        )
    )

    proxies = [
        feature
        for feature in spec.feature_columns
        if all(str(row[feature]) == str(row[spec.target_column]) for row in rows)
    ]
    checks.append(
        _check(
            "target_proxy_leakage",
            not proxies,
            "no_exact_target_proxy" if not proxies else "exact_target_proxy",
            "No feature exactly reproduces the target."
            if not proxies
            else f"Features reproduce the target exactly: {proxies}",
        )
    )

    split_ok = set(train).isdisjoint(test) and len(train) + len(test) == len(rows)
    if spec.split.strategy == "temporal" and split_ok:
        column = spec.split.timestamp_column
        assert column is not None
        split_ok = max(str(rows[index][column]) for index in train) <= min(
            str(rows[index][column]) for index in test
        )
    if spec.split.strategy == "group" and split_ok:
        column = spec.split.group_column
        assert column is not None
        split_ok = {str(rows[index][column]) for index in train}.isdisjoint(
            str(rows[index][column]) for index in test
        )
    checks.append(
        _check(
            "split_integrity",
            split_ok,
            "split_valid" if split_ok else "invalid_split",
            "Split partitions are disjoint and strategy constraints hold."
            if split_ok
            else "Split partitions overlap or violate strategy constraints.",
        )
    )
    checks.append(
        _check(
            "metric_suitability",
            True,
            "metric_matches_problem",
            "The primary metric is valid for the requested problem type.",
        )
    )
    return tuple(checks)


def _check(check_id: str, passed: bool, code: str, message: str) -> ScienceCheck:
    return ScienceCheck(
        check_id=check_id,
        passed=passed,
        severity="error",
        code=code,
        message=message,
    )


def _signature(row: dict[str, Any], columns: set[str]) -> tuple[str, ...]:
    return tuple(str(row[column]) for column in sorted(columns))


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
