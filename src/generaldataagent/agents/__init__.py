"""Deep Agents role configuration exports."""

from generaldataagent.agents.contracts import SpecialistResult, SupervisorResult
from generaldataagent.agents.factory import build_supervisor
from generaldataagent.agents.metric_supervisor import MetricSupervisorRuntime
from generaldataagent.agents.rule_supervisor import RuleSupervisorRuntime
from generaldataagent.agents.specs import SPECIALIST_SPECS, SpecialistSpec
from generaldataagent.agents.supervision import NoToolArguments, record_role

__all__ = [
    "SPECIALIST_SPECS",
    "MetricSupervisorRuntime",
    "NoToolArguments",
    "RuleSupervisorRuntime",
    "SpecialistResult",
    "SpecialistSpec",
    "SupervisorResult",
    "build_supervisor",
    "record_role",
]
