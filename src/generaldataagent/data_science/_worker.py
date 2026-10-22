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
        deltas = {
            label: train_counts[label] / len(train) - test_counts[label] / len(test)
            for label in labels
        }
        label = max(deltas, key=lambda item: abs(deltas[item]))
        pooled = (train_counts[label] + test_counts[label]) / (len(train) + len(test))
        standard_error = math.sqrt(pooled * (1 - pooled) * (1 / len(train) + 1 / len(test)))
        statistic = deltas[label] / standard_error if standard_error else 0.0
        return {
            "measure": "maximum_class_proportion_delta",
            "value": abs(deltas[label]),
            "label": label,
            "normal_approximation_statistic": statistic,
            "two_sided_normal_p_value": math.erfc(abs(statistic) / math.sqrt(2)),
        }
    train_values = [float(row[target]) for row in train]
    test_values = [float(row[target]) for row in test]
    combined = train_values + test_values
    scale = statistics.pstdev(combined)
    value = abs(statistics.fmean(train_values) - statistics.fmean(test_values))
    train_variance = statistics.variance(train_values) if len(train_values) > 1 else 0.0
    test_variance = statistics.variance(test_values) if len(test_values) > 1 else 0.0
    standard_error = math.sqrt(
        train_variance / len(train_values) + test_variance / len(test_values)
    )
    statistic = value / standard_error if standard_error else 0.0
    return {
        "measure": "target_standardized_mean_difference",
        "value": value / scale if scale else 0.0,
        "normal_approximation_statistic": statistic,
        "two_sided_normal_p_value": math.erfc(abs(statistic) / math.sqrt(2)),
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


def _subgroup_evaluation(test, spec, prediction):
    results = []
    for column in spec["subgroup_columns"]:
        values = sorted({str(row[column]) for row in test})
        for value in values:
            group = [row for row in test if str(row[column]) == value]
            if spec["problem_type"] == "binary_classification":
                metric_name = "accuracy"
                metric_value = sum(str(row[spec["target_column"]]) == prediction for row in group)
                metric_value /= len(group)
            else:
                metric_name = "mae"
                metric_value = statistics.fmean(
                    abs(float(row[spec["target_column"]]) - prediction) for row in group
                )
            results.append(
                {
                    "column": column,
                    "value": value,
                    "row_count": len(group),
                    "metrics": {metric_name: metric_value},
                }
            )
    return results


def _least_squares(matrix, target):
    """Householder QR; rejects rank deficiency instead of unstable normal equations."""
    a = [list(map(float, row)) for row in matrix]
    y = list(map(float, target))
    n, p = len(a), len(a[0])
    scale = max(abs(v) for row in a for v in row)
    for k in range(p):
        norm = math.hypot(*(a[i][k] for i in range(k, n)))
        if norm <= 1e-12 * max(1.0, scale):
            raise ValueError("rank deficient design matrix")
        alpha = -math.copysign(norm, a[k][k])
        vector = [a[i][k] for i in range(k, n)]
        vector[0] -= alpha
        magnitude = math.hypot(*vector)
        vector = [v / magnitude for v in vector]
        for j in range(k, p):
            dot = math.fsum(v * a[i][j] for i, v in zip(range(k, n), vector, strict=True))
            for i, v in zip(range(k, n), vector, strict=True):
                a[i][j] -= 2 * v * dot
        dot = math.fsum(v * y[i] for i, v in zip(range(k, n), vector, strict=True))
        for i, v in zip(range(k, n), vector, strict=True):
            y[i] -= 2 * v * dot
    coefficients = [0.0] * p
    for i in reversed(range(p)):
        coefficients[i] = (
            y[i] - math.fsum(a[i][j] * coefficients[j] for j in range(i + 1, p))
        ) / a[i][i]
    return coefficients


def _linear_regression(train, test, spec):
    features, target = spec["feature_columns"], spec["target_column"]
    means, scales = {}, {}
    for c in features:
        values = [float(row[c]) for row in train if row[c] is not None]
        if not values:
            raise ValueError("feature has no observed training values")
        means[c] = statistics.fmean(values)
        imputed = [means[c] if row[c] is None else float(row[c]) for row in train]
        scales[c] = (statistics.pstdev(imputed) or 1.0) if spec["standardize"] else 1.0

    def transform(rows):
        return [
            [1.0]
            + [
                (
                    (means[c] if row[c] is None else float(row[c]))
                    - (means[c] if spec["standardize"] else 0)
                )
                / scales[c]
                for c in features
            ]
            for row in rows
        ]

    if spec["missing_features"] == "raise" and any(
        row[c] is None for row in train + test for c in features
    ):
        raise ValueError("missing feature values")
    coefficient = _least_squares(transform(train), [r[target] for r in train])
    predictions = [
        math.fsum(a * b for a, b in zip(row, coefficient, strict=True)) for row in transform(test)
    ]
    actual = [float(row[target]) for row in test]
    errors = [a - p for a, p in zip(actual, predictions, strict=True)]
    denominator = math.fsum((a - statistics.fmean(actual)) ** 2 for a in actual)
    if denominator == 0:
        raise ValueError("held-out R2 undefined for constant target")
    metrics = {
        "mae": statistics.fmean(abs(e) for e in errors),
        "rmse": math.sqrt(statistics.fmean(e * e for e in errors)),
        "r2": 1 - math.fsum(e * e for e in errors) / denominator,
    }
    raw_coefficient = {c: coefficient[i + 1] / scales[c] for i, c in enumerate(features)}
    intercept = coefficient[0] - (
        math.fsum(raw_coefficient[c] * means[c] for c in features) if spec["standardize"] else 0
    )
    model = {
        "algorithm": "linear_regression_householder_qr",
        "coefficients": raw_coefficient,
        "intercept": intercept,
        "preprocessing": {
            "fit_scope": "training_indices_only",
            "imputation_means": means,
            "scales": scales,
            "standardize": spec["standardize"],
        },
        "parameters": {"random_seed": spec["random_seed"], "feature_columns": features},
    }
    baseline_metrics, _ = _regression(train, test, target)
    return (
        metrics,
        {
            "baseline_prediction": statistics.fmean(r[target] for r in train),
            "baseline_metrics": baseline_metrics,
            "predictions": predictions,
            "mean_error": statistics.fmean(errors),
            "maximum_absolute_error": max(abs(e) for e in errors),
        },
        model,
    )


def execute(payload):
    rows = payload["rows"]
    train = [rows[index] for index in payload["train_indices"]]
    test = [rows[index] for index in payload["test_indices"]]
    spec = payload["spec"]
    linear = spec.get("estimator") == "linear_regression"
    if linear:
        metrics, errors, model = _linear_regression(train, test, spec)
    elif spec["problem_type"] == "binary_classification":
        metrics, errors = _classification(train, test, spec["target_column"])
    else:
        metrics, errors = _regression(train, test, spec["target_column"])
    result = {
        "template_version": "2.0" if linear else TEMPLATE_VERSION,
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
        "subgroups": _subgroup_evaluation(test, spec, errors["baseline_prediction"]),
        "model": {
            "algorithm": spec["baseline"],
            "parameters": {"random_seed": spec["random_seed"]},
        },
    }
    if linear:
        result["model"] = model
        result["statistical_checks"]["split_manifest"] = {
            "train_indices": payload["train_indices"],
            "test_indices": payload["test_indices"],
            "strategy": spec["split"]["strategy"],
            "random_seed": spec["random_seed"],
        }
        for subgroup in result["subgroups"]:
            positions = [
                i for i, row in enumerate(test) if str(row[subgroup["column"]]) == subgroup["value"]
            ]
            subgroup["metrics"]["mae"] = statistics.fmean(
                abs(float(test[i][spec["target_column"]]) - errors["predictions"][i])
                for i in positions
            )
    return result


def notebook_sources(payload):
    from pathlib import Path

    source = Path(__file__).read_text().split('\nif __name__ == "__main__":')[0]
    cell = (
        "payload = json.loads(" + repr(json.dumps(payload, sort_keys=True, allow_nan=False)) + ")\n"
    )
    cell += "result = execute(payload)\nprint(json.dumps(result, sort_keys=True, allow_nan=False))"
    return source, cell


def execute_notebook(notebook):
    # Only the exact repository template and a JSON literal are executable. User
    # edits, extra cells and alternate source are rejected before compilation.
    payload = notebook["metadata"]["generaldataagent"]["payload"]
    expected = notebook_sources(payload)
    cells = notebook["cells"]
    if len(cells) != 2 or any(
        c.get("cell_type") != "code" or c.get("source") != source
        for c, source in zip(cells, expected, strict=True)
    ):
        raise ValueError("notebook is not the fixed repository template")
    namespace = {"__name__": "gda_fixed_notebook", "__file__": __file__}
    for cell in cells:
        exec(compile(cell["source"], "<fixed-stage8-notebook>", "exec"), namespace)
    return namespace["result"]


def main():
    import contextlib
    import io

    payload = json.load(sys.stdin)
    if "notebook" in payload:
        with contextlib.redirect_stdout(io.StringIO()):
            result = execute_notebook(payload["notebook"])
    else:
        result = execute(payload)
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))


if __name__ == "__main__":
    main()
