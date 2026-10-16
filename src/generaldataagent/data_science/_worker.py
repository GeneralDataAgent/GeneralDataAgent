"""Fixed Stage 3 experiment template; never evaluates user-supplied code."""

import json
import math
import statistics
import sys
from collections import Counter

TEMPLATE_VERSION = "1.0"


def _numeric_summary(rows, features):
    result = {}
    for feature in features:
        values = [row[feature] for row in rows if isinstance(row.get(feature), (int, float))]
        if values:
            result[feature] = {
                "count": len(values),
                "minimum": min(values),
                "maximum": max(values),
                "mean": statistics.fmean(values),
            }
    return result


def _standardized_mean_differences(train, test, features):
    result = {}
    for feature in features:
        train_values = [
            float(row[feature]) for row in train if isinstance(row.get(feature), (int, float))
        ]
        test_values = [
            float(row[feature]) for row in test if isinstance(row.get(feature), (int, float))
        ]
        if train_values and test_values:
            train_mean = statistics.fmean(train_values)
            test_mean = statistics.fmean(test_values)
            combined = train_values + test_values
            scale = statistics.pstdev(combined)
            result[feature] = abs(train_mean - test_mean) / scale if scale else 0.0
    return result


def _target_shift(train, test, target, problem_type):
    if problem_type == "binary_classification":
        train_counts = Counter(str(row[target]) for row in train)
        test_counts = Counter(str(row[target]) for row in test)
        labels = set(train_counts) | set(test_counts)
        return {
            "measure": "maximum_class_proportion_delta",
            "value": max(
                abs(train_counts[label] / len(train) - test_counts[label] / len(test))
                for label in labels
            ),
        }
    train_values = [float(row[target]) for row in train]
    test_values = [float(row[target]) for row in test]
    combined = train_values + test_values
    scale = statistics.pstdev(combined)
    value = abs(statistics.fmean(train_values) - statistics.fmean(test_values))
    return {
        "measure": "target_standardized_mean_difference",
        "value": value / scale if scale else 0.0,
    }


def _classification(train, test, target):
    counts = Counter(str(row[target]) for row in train)
    labels = sorted(counts)
    prediction = min(labels, key=lambda label: (-counts[label], label))
    positive = labels[-1]
    actual = [str(row[target]) for row in test]
    predicted = [prediction] * len(test)
    tp = sum(a == positive and p == positive for a, p in zip(actual, predicted, strict=True))
    fp = sum(a != positive and p == positive for a, p in zip(actual, predicted, strict=True))
    fn = sum(a == positive and p != positive for a, p in zip(actual, predicted, strict=True))
    accuracy = sum(a == p for a, p in zip(actual, predicted, strict=True)) / len(actual)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return (
        {"accuracy": accuracy, "precision": precision, "recall": recall, "f1": f1},
        {
            "baseline_prediction": prediction,
            "positive_label": positive,
            "false_positives": fp,
            "false_negatives": fn,
        },
    )


def _regression(train, test, target):
    train_values = [float(row[target]) for row in train]
    actual = [float(row[target]) for row in test]
    prediction = statistics.fmean(train_values)
    errors = [value - prediction for value in actual]
    mae = statistics.fmean(abs(error) for error in errors)
    rmse = math.sqrt(statistics.fmean(error * error for error in errors))
    baseline = statistics.fmean(actual)
    denominator = sum((value - baseline) ** 2 for value in actual)
    r2 = 1 - sum(error * error for error in errors) / denominator if denominator else 0.0
    return (
        {"mae": mae, "rmse": rmse, "r2": r2},
        {
            "baseline_prediction": prediction,
            "mean_error": statistics.fmean(errors),
            "maximum_absolute_error": max(abs(error) for error in errors),
        },
    )


def main():
    payload = json.load(sys.stdin)
    rows = payload["rows"]
    train = [rows[index] for index in payload["train_indices"]]
    test = [rows[index] for index in payload["test_indices"]]
    spec = payload["spec"]
    if spec["problem_type"] == "binary_classification":
        metrics, errors = _classification(train, test, spec["target_column"])
    else:
        metrics, errors = _regression(train, test, spec["target_column"])
    result = {
        "template_version": TEMPLATE_VERSION,
        "train_rows": len(train),
        "test_rows": len(test),
        "metrics": metrics,
        "eda": {
            "row_count": len(rows),
            "missing_by_column": {
                column: sum(row.get(column) is None for row in rows)
                for column in [spec["target_column"], *spec["feature_columns"]]
            },
            "numeric_features": _numeric_summary(rows, spec["feature_columns"]),
        },
        "statistical_checks": {
            "train_target_distribution": dict(
                sorted(Counter(str(row[spec["target_column"]]) for row in train).items())
            ),
            "test_target_distribution": dict(
                sorted(Counter(str(row[spec["target_column"]]) for row in test).items())
            ),
            "feature_standardized_mean_difference": _standardized_mean_differences(
                train, test, spec["feature_columns"]
            ),
            "target_shift": _target_shift(train, test, spec["target_column"], spec["problem_type"]),
        },
        "error_analysis": errors,
        "model": {
            "algorithm": spec["baseline"],
            "parameters": {"random_seed": spec["random_seed"]},
        },
    }
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
