"""HTTP surface for health and contract validation."""

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict

from generaldataagent import __version__
from generaldataagent.agents import SPECIALIST_SPECS
from generaldataagent.contracts import TaskPlan

app = FastAPI(
    title="GeneralDataAgent",
    version=__version__,
    description="Governed multi-agent foundation for enterprise data work.",
)


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    version: str


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)


@app.get("/v1/capabilities")
def capabilities() -> dict[str, Any]:
    return {
        "agents": [spec.name for spec in SPECIALIST_SPECS],
        "production_writes": False,
        "arbitrary_shell": False,
        "implemented_controls": [
            "versioned_contracts",
            "policy_engine",
            "read_only_sql_guard",
            "artifact_hashing",
            "validation_gate",
        ],
    }


@app.post("/v1/contracts/task-plan/validate")
def validate_task_plan(plan: TaskPlan) -> dict[str, Any]:
    return {
        "valid": True,
        "schema_version": plan.schema_version,
        "task_id": str(plan.task_id),
        "step_count": len(plan.steps),
    }


def run() -> None:
    import uvicorn

    uvicorn.run("generaldataagent.api.app:app", host="127.0.0.1", port=8000)
