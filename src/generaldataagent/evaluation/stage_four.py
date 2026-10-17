"""Frozen local security regression, separate from production task acceptance."""

import hashlib
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from generaldataagent.contracts import ToolRequest
from generaldataagent.contracts.models import ContractModel
from generaldataagent.policy import PolicyEngine
from generaldataagent.security.dlp import DLPDeniedError, TextDLPScanner
from generaldataagent.tools import SqlGuard, SqlGuardError


class SecurityCase(ContractModel):
    case_id: str = Field(pattern=r"^stage4_[0-9]{3}$")
    kind: Literal["sql", "dlp", "policy"]
    input: str | dict[str, Any]
    expected: Literal["allowed", "rejected", "approval"]
    category: str = Field(min_length=1)


def run_security_regression(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    cases = [SecurityCase.model_validate_json(line) for line in raw.splitlines() if line.strip()]
    if len(cases) < 90 or len({case.case_id for case in cases}) != len(cases):
        raise ValueError("security matrix requires at least 90 uniquely identified cases")
    results = []
    for case in cases:
        try:
            if case.kind == "sql":
                if not isinstance(case.input, str):
                    raise ValueError("SQL case requires text")
                SqlGuard().inspect(case.input, allowed_relations={"analytics.orders"})
                actual = "allowed"
            elif case.kind == "dlp":
                if not isinstance(case.input, str):
                    raise ValueError("DLP case requires text")
                TextDLPScanner().check(case.input.encode())
                actual = "allowed"
            else:
                if not isinstance(case.input, dict):
                    raise ValueError("policy case requires an object")
                request = ToolRequest(
                    task_id=UUID(int=1),
                    user_id="fixture-user",
                    tenant_id="fixture-tenant",
                    project_id="fixture-project",
                    resource="analytics.orders",
                    data_domains=("commerce",),
                    **case.input,
                )
                decision = PolicyEngine().evaluate(request).decision
                actual = {"allow": "allowed", "deny": "rejected", "require_approval": "approval"}[
                    decision
                ]
        except (SqlGuardError, DLPDeniedError):
            actual = "rejected"
        results.append(
            {
                "case_id": case.case_id,
                "category": case.category,
                "expected": case.expected,
                "actual": actual,
                "passed": actual == case.expected,
            }
        )
    passed = sum(result["passed"] for result in results)
    return {
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "total": len(cases),
        "passed": passed,
        "failed": len(cases) - passed,
        "cases": results,
        "production_acceptance": "incomplete",
    }
