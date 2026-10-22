"""Research-tool adapter to the same fixed, bounded Stage 3/8 experiment worker."""

import hashlib
import json
from uuid import uuid4

from generaldataagent.contracts import ExperimentSpec
from generaldataagent.data_science.checks import split_indices, validate_experiment_data
from generaldataagent.data_science.sandbox import FixedTemplateSandbox
from generaldataagent.data_science.statistics import derive_features


def evaluate_regression(rows, request):
    derived = derive_features(rows, request.derived)
    spec = ExperimentSpec(
        task_id=uuid4(),
        snapshot_id=uuid4(),
        problem_type="regression",
        target_column=request.target,
        feature_columns=tuple(request.features),
        baseline="mean",
        estimator="linear_regression",
        primary_metric="r2",
        split=request.split,
        random_seed=request.random_seed,
        missing_features=request.missing_features,
        standardize=request.standardize,
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
