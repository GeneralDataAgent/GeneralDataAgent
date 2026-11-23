"""Frozen business-context binding for durable metric tasks.

A task may explicitly pin one confirmed business-context version at acceptance. The
binding is metadata only: it is never injected into the graph, prompts, SQL, tool
policies, budgets or approvals. Its purpose is to make later citation and
consistency checks possible without silently switching to newer memory.
"""

from typing import Literal

from pydantic import Field

from generaldataagent.contracts.models import ContractModel


class TaskContextBinding(ContractModel):
    """An opt-in pointer to one already confirmed business-context version."""

    schema_version: Literal["1.0"] = "1.0"
    context_version: int = Field(ge=1, le=2**31 - 1, strict=True)
