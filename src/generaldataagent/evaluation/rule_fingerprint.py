"""Explicit source inventory for development rule workflow qualification.

This identifies selected implementation files, not a signed build or a complete
transitive dependency lock. Read failures propagate instead of omitting a source.
"""

import hashlib
from pathlib import Path

_WORKFLOW_SOURCES = (
    "evaluation/rule_fingerprint.py",
    "business/rules/workflow.py",
    "business/rules/engine.py",
    "business/rules/application.py",
    "business/rules/payments.py",
    "business/rules/retrieval.py",
    "business/rules/verification.py",
    "evaluation/stage_nine.py",
    "contracts/models.py",
    "contracts/rules.py",
    "contracts/rule_tools.py",
    "orchestration/rule_workflow.py",
    "orchestration/checkpoints.py",
    "tools/sqlite.py",
    "tools/sql_guard.py",
)
_GOVERNANCE_SOURCES = (
    "business/rules/governed.py",
    "tools/gateway.py",
    "policy/engine.py",
    "policy/approvals.py",
    "policy/budgets.py",
    "artifacts/local.py",
    "observability/audit.py",
)


def rule_workflow_fingerprint(*, governed: bool = False) -> dict[str, str]:
    """Hash package source files used by the workflow and optional gateway."""
    if type(governed) is not bool:
        raise ValueError("rule fingerprint governed must be boolean")
    package = Path(__file__).resolve().parents[1]
    paths = _WORKFLOW_SOURCES + (_GOVERNANCE_SOURCES if governed else ())
    return {name: hashlib.sha256((package / name).read_bytes()).hexdigest() for name in paths}
