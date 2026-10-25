"""One preparation and execution path for fixed-template experiments.

The governed workbench and the regression analysis adapter must split the same
rows, run the same leakage checks and hand the same worker the same payload.
Keeping that sequence in two places let them drift: the adapter could skip a
check the product path already enforced, and nothing in either caller made the
split deterministic or the checks comparable.

The *gate* stays with each caller, deliberately. The workbench raises
``ScienceGateError`` carrying every check; the evaluation adapter raises a
value error naming the failed codes, which is what its transport reports. Only
the shared sequence moves here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from generaldataagent.contracts import ExperimentSpec, ScienceCheck
from generaldataagent.data_science.checks import split_indices, validate_experiment_data
from generaldataagent.data_science.sandbox import FixedTemplateSandbox, OCIContainerSandbox

ExperimentSandbox = FixedTemplateSandbox | OCIContainerSandbox


@dataclass(frozen=True, slots=True)
class PreparedExperiment:
    """A split, its checks and the exact rows the worker will receive."""

    rows: tuple[dict[str, Any], ...]
    spec: ExperimentSpec
    train: list[int]
    test: list[int]
    checks: tuple[ScienceCheck, ...]

    @property
    def blocked(self) -> bool:
        """Any error-severity check means the experiment must not run."""
        return any(not check.passed and check.severity == "error" for check in self.checks)


def prepare_experiment(
    rows: list[dict[str, Any]] | tuple[dict[str, Any], ...], spec: ExperimentSpec
) -> PreparedExperiment:
    """Split and validate once; the caller decides how a failed gate is reported."""
    prepared_rows = tuple(dict(row) for row in rows)
    train, test = split_indices(list(prepared_rows), spec)
    checks = validate_experiment_data(list(prepared_rows), spec, train, test)
    return PreparedExperiment(rows=prepared_rows, spec=spec, train=train, test=test, checks=checks)


def resolve_sandbox(sandbox: ExperimentSandbox | None = None) -> ExperimentSandbox:
    """The runner this call will actually use; never a second, implicit one."""
    return sandbox or FixedTemplateSandbox()


def run_prepared_experiment(
    prepared: PreparedExperiment, *, sandbox: ExperimentSandbox | None = None
) -> dict[str, Any]:
    """Run the fixed worker; an injected sandbox replaces the local default."""
    runner = resolve_sandbox(sandbox)
    return runner.run(
        rows=[dict(row) for row in prepared.rows],
        spec=prepared.spec,
        train_indices=list(prepared.train),
        test_indices=list(prepared.test),
    )


__all__ = [
    "ExperimentSandbox",
    "PreparedExperiment",
    "prepare_experiment",
    "resolve_sandbox",
    "run_prepared_experiment",
]
