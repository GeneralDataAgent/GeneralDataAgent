"""Deep Agents role configuration exports."""

from generaldataagent.agents.factory import build_supervisor
from generaldataagent.agents.specs import SPECIALIST_SPECS, SpecialistSpec

__all__ = ["SPECIALIST_SPECS", "SpecialistSpec", "build_supervisor"]
