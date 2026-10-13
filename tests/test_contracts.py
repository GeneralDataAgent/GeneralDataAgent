from uuid import uuid4

import pytest
from pydantic import ValidationError

from generaldataagent.contracts import (
    AgentRole,
    TaskPlan,
    TaskStep,
    ValidationCheck,
    ValidationResult,
    ValidationStatus,
)


def test_task_plan_accepts_topologically_ordered_steps() -> None:
    plan = TaskPlan(
        user_id="user-1",
        tenant_id="tenant-1",
        project_id="project-1",
        objective="Analyze weekly conversion",
        steps=(
            TaskStep(
                step_id="discover",
                objective="Resolve the certified metric",
                agent_role=AgentRole.DISCOVERY,
            ),
            TaskStep(
                step_id="analyze",
                objective="Run read-only analysis",
                agent_role=AgentRole.DATA_ANALYST,
                depends_on=("discover",),
            ),
        ),
    )
    assert len(plan.steps) == 2


def test_task_plan_rejects_forward_dependency() -> None:
    with pytest.raises(ValidationError, match="unknown or forward dependencies"):
        TaskPlan(
            user_id="user-1",
            tenant_id="tenant-1",
            project_id="project-1",
            objective="Invalid plan",
            steps=(
                TaskStep(
                    step_id="analyze",
                    objective="Analyze before discovery",
                    agent_role=AgentRole.DATA_ANALYST,
                    depends_on=("discover",),
                ),
                TaskStep(
                    step_id="discover",
                    objective="Discover data",
                    agent_role=AgentRole.DISCOVERY,
                ),
            ),
        )


def test_validation_cannot_pass_with_failed_check() -> None:
    with pytest.raises(ValidationError, match="cannot pass"):
        ValidationResult(
            task_id=uuid4(),
            status=ValidationStatus.PASS,
            verifier="verifier",
            checks=(
                ValidationCheck(
                    check_id="grounding",
                    passed=False,
                    code="missing_evidence",
                    message="A claim has no evidence.",
                ),
            ),
        )
