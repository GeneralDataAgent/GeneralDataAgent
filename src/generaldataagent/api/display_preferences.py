"""Creator-private explicit display preferences and completed-result presentation."""

from collections.abc import Callable
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, Query, Request

from generaldataagent.memory.display_preferences import (
    ConfirmDisplayPreference,
    DisplayPreferenceRecord,
    DisplayPreferenceState,
    PreferenceConflict,
    PreferenceUnavailable,
)
from generaldataagent.orchestration.task_presentation import (
    MetricDisplayPreferenceService,
    MetricTaskPresentation,
)
from generaldataagent.orchestration.task_queue import TaskQueueConflict


def display_preference_router(
    service: MetricDisplayPreferenceService | None, credentials: Callable
) -> APIRouter:
    router = APIRouter()

    def invoke(request, action):
        if service is None:
            raise HTTPException(status_code=503, detail="display_preferences_not_configured")
        actor, token = credentials(request)
        try:
            return action(service, actor, token)
        except PermissionError as exc:
            raise HTTPException(status_code=404, detail="display_preference_unavailable") from exc
        except (PreferenceConflict, TaskQueueConflict) as exc:
            raise HTTPException(status_code=409, detail="display_preference_conflict") from exc
        except (PreferenceUnavailable, ValueError) as exc:
            raise HTTPException(status_code=503, detail="display_preference_unavailable") from exc

    path = "/v1/analysis/preferences/display"

    @router.get(path, response_model=DisplayPreferenceState)
    def current(request: Request):
        return invoke(request, lambda s, a, t: s.current(a, t))

    @router.put(path, response_model=DisplayPreferenceState)
    def confirm(request: Request, body: ConfirmDisplayPreference):
        return invoke(request, lambda s, a, t: s.confirm(a, t, body))

    @router.delete(path, response_model=DisplayPreferenceState)
    def revoke(request: Request, expected_version: int = Query(ge=0, le=2**31 - 1)):
        return invoke(request, lambda s, a, t: s.revoke(a, t, expected_version))

    @router.get(path + "/versions/{version}", response_model=DisplayPreferenceRecord)
    def history(request: Request, version: int = Path(ge=1, le=2**31 - 1)):
        return invoke(request, lambda s, a, t: s.history(a, t, version))

    @router.get("/v1/analysis/tasks/{task_id}/presentation", response_model=MetricTaskPresentation)
    def presentation(request: Request, task_id: UUID):
        return invoke(request, lambda s, a, t: s.presentation(task_id, a, t))

    return router
