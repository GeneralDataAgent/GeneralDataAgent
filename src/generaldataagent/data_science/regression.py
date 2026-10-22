"""Research-tool adapter to the same fixed, bounded Stage 3/8 experiment worker."""

import hashlib
import json
import re
from uuid import uuid4

from generaldataagent.contracts import ExperimentSpec, SandboxQuota
from generaldataagent.data_science.checks import split_indices, validate_experiment_data
from generaldataagent.data_science.sandbox import FixedTemplateSandbox
from generaldataagent.data_science.statistics import derive_features, finite_number


def requests_sklearn(text):
    return bool(re.search(r"\b(?:scikit[ -]?learn|sklearn)\b", text, re.I))


def sklearn_bindings(bindings, evidence):
    return bool(bindings) and all(
        evidence.get(step, {}).get("model", {}).get("algorithm")
        == "sklearn.linear_model.LinearRegression"
        and evidence.get(step, {}).get("model", {}).get("sklearn_version")
        for step, _ in bindings.values()
    )


def requested_regression_metrics(text):
    if not re.search(r"\b(?:regression|train\w*|machine learning|model)\b", text, re.I):
        return set()
    metrics = set()
    if re.search(r"root\s+mean\s+squared\s+error|\bRMSE\b", text, re.I):
        metrics.add("rmse")
    without_root = re.sub(r"root\s+mean\s+squared\s+error", "", text, flags=re.I)
    if re.search(r"mean\s+squared\s+error|\bMSE\b", without_root, re.I):
        metrics.add("mse")
    if re.search(r"mean\s+absolute\s+error|\bMAE\b", text, re.I):
        metrics.add("mae")
    if re.search(r"\br[ -]?squared\b|\br2\b|r\^2", text, re.I):
        metrics.add("r2")
    return metrics


def regression_metric_bindings(bindings, evidence, text):
    requested = requested_regression_metrics(text)
    if len(requested) != 1:
        return True
    metric = next(iter(requested))
    return any(
        evidence.get(step, {}).get("method_id") == "regression_evaluation"
        and path == ["metrics", metric]
        for step, path in bindings.values()
    )


def evaluate_regression(rows, request, *, public_text=""):
    requested_metrics = requested_regression_metrics(public_text)
    if len(requested_metrics) == 1 and request.primary_metric not in requested_metrics:
        raise ValueError("primary metric conflicts with public regression metric")
    if requests_sklearn(public_text) and request.estimator != "sklearn_linear_regression":
        raise ValueError("public sklearn requirement cannot use the stdlib estimator")
    named_seeds = {
        int(v)
        for v in re.findall(
            r"\brandom[ _](?:state|seed)\s*(?:(?:of|to|is|=|:)\s*)?(\d+)", public_text, re.I
        )
    }
    if named_seeds and named_seeds != {request.random_seed}:
        raise ValueError("experiment seed conflicts with public named seed")
    derived = derive_features(rows, request.derived)
    source_indices = list(range(len(derived)))
    if request.missing_rows == "drop":
        source_indices = [
            i
            for i, row in enumerate(derived)
            if all(finite_number(row[c]) for c in request.features + [request.target])
        ]
        derived = [derived[i] for i in source_indices]
    spec = ExperimentSpec(
        task_id=uuid4(),
        snapshot_id=uuid4(),
        problem_type="regression",
        target_column=request.target,
        feature_columns=tuple(request.features),
        baseline="mean",
        estimator=request.estimator,
        primary_metric=request.primary_metric,
        split=request.split,
        random_seed=request.random_seed,
        missing_features=request.missing_features,
        standardize=request.standardize,
        duplicate_policy=request.duplicate_policy,
        quota=SandboxQuota(
            memory_mb=512 if request.estimator == "sklearn_linear_regression" else 256
        ),
    )
    train, test = split_indices(derived, spec)
    checks = validate_experiment_data(derived, spec, train, test)
    if any(not c.passed and c.severity == "error" for c in checks):
        raise ValueError(
            "regression checks failed: " + ",".join(c.code for c in checks if not c.passed)
        )
    runner = FixedTemplateSandbox()
    result = runner.run(rows=derived, spec=spec, train_indices=train, test_indices=test)
    result.update(
        operation="regression_evaluation",
        source_indices=source_indices,
        dropped_rows=len(rows) - len(derived),
        checks=[c.model_dump(mode="json") for c in checks],
        source_hash="sha256:"
        + hashlib.sha256(json.dumps(rows, sort_keys=True, allow_nan=False).encode()).hexdigest(),
        derived_hash="sha256:"
        + hashlib.sha256(json.dumps(derived, sort_keys=True, allow_nan=False).encode()).hexdigest(),
        runtime_fingerprint=runner.runtime_fingerprint,
        scope=(
            "held-out predictive evaluation; seed and method defaults declared, "
            "not inferred from labels"
        ),
    )
    return result
