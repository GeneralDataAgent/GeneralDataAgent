"""Deep Agents role configuration exports."""

from generaldataagent.agents.contracts import SpecialistResult, SupervisorResult
from generaldataagent.agents.factory import build_supervisor
from generaldataagent.agents.metric_supervisor import MetricSupervisorRuntime
from generaldataagent.agents.specs import SPECIALIST_SPECS, SpecialistSpec

__all__ = [
    "SPECIALIST_SPECS",
    "MetricSupervisorRuntime",
    "SpecialistResult",
    "SpecialistSpec",
    "SupervisorResult",
    "build_supervisor",
]
