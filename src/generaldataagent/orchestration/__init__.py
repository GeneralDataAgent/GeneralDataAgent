"""Workflow exports.

Every name except the composition values is exported lazily, because importing a
submodule runs this package's ``__init__``. ``workflow`` needs LangGraph and
``metric_analysis`` needs the LLM stack; the composition and its identity are
imported by modules that have nothing to do with model execution (the science
workbench, for one), and importing those values must not pull either in.
"""

from typing import TYPE_CHECKING, Any

from generaldataagent.orchestration.composition import (
    CompositionError,
    CompositionMode,
    ExecutionComposition,
    ExecutionIdentity,
)

if TYPE_CHECKING:
    from generaldataagent.orchestration.metric_analysis import MetricAnalysisRuntime
    from generaldataagent.orchestration.workflow import PhaseOneServices, build_phase_one_graph

__all__ = [
    "CompositionError",
    "CompositionMode",
    "ExecutionComposition",
    "ExecutionIdentity",
    "MetricAnalysisRuntime",
    "PhaseOneServices",
    "build_phase_one_graph",
]

_LAZY = {
    "MetricAnalysisRuntime": "generaldataagent.orchestration.metric_analysis",
    "PhaseOneServices": "generaldataagent.orchestration.workflow",
    "build_phase_one_graph": "generaldataagent.orchestration.workflow",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(module_name), name)
    globals()[name] = value  # Resolve once; later lookups skip this function.
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
