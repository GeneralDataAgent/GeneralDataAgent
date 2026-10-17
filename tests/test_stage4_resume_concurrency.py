from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from generaldataagent.api.app import create_app
from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.catalog import InMemoryMetricCatalog
from generaldataagent.contracts import ActorContext
from generaldataagent.orchestration import (
    MetricAnalysisRuntime,
    PhaseOneServices,
    build_phase_one_graph,
)
from generaldataagent.orchestration.checkpoints import (
    CheckpointBusyError,
    CheckpointLeaseLostError,
    open_protected_checkpointer,
)
from generaldataagent.security.identity import SessionBroker


def test_parallel_resume_is_rejected_without_replaying_analysis(tmp_path):
    actor = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("d",))
    path, thread = tmp_path / "checkpoints", str(uuid4())
    started, release = Event(), Event()
    calls = []

    def analyze(state):
        calls.append(state["task_id"])
        started.set()
        assert release.wait(5)
        return {"analysis_result": {"count": 1}}

    services = PhaseOneServices(
        discover=lambda _: {},
        plan=lambda _: {},
        analyze=analyze,
        validate=lambda _: {"validation_result": {"status": "pass"}},
        deliver=lambda _: {"status": "completed"},
    )
    with (
        open_protected_checkpointer(path, encryption_key=b"k" * 32) as first,
        open_protected_checkpointer(path, encryption_key=b"k" * 32) as second,
    ):
        first.register_thread(thread, actor)
        prepare = build_phase_one_graph(services, checkpointer=first)
        prepare.invoke(
            {"task_id": thread, "actor_context": actor.model_dump(mode="json")},
            {"configurable": {"thread_id": thread}},
            interrupt_after=["plan"],
        )

        def runtime(saver):
            value = MetricAnalysisRuntime(
                catalog=InMemoryMetricCatalog([]),
                executor=None,
                artifacts=LocalArtifactStore(tmp_path / "artifacts"),
                checkpointer=saver,
            )
            value.graph = build_phase_one_graph(services, checkpointer=saver)
            return value

        left, right = runtime(first), runtime(second)
        broker = SessionBroker()
        token = broker.issue(
            user_id="u",
            tenant_id="t",
            project_id="p",
            data_domains=frozenset({"d"}),
            permissions=frozenset({"analysis:run"}),
        )
        api = TestClient(create_app(metric_runtime=right, session_broker=broker))
        headers = {
            "authorization": f"Bearer {token}",
            "x-user-id": "u",
            "x-tenant-id": "t",
            "x-project-id": "p",
            "x-data-domain": "d",
        }
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(left.resume, thread, actor)
            try:
                assert started.wait(3)
                response = api.post(f"/v1/analysis/tasks/{thread}/resume", headers=headers)
                assert response.status_code == 409
                assert response.json()["detail"] == "checkpoint_already_running"
            finally:
                release.set()
            assert future.result(timeout=5)["status"] == "completed"
        assert right.resume(thread, actor)["status"] == "completed"
        assert calls == [thread]


def test_stale_execution_lease_can_be_reclaimed_and_old_owner_cannot_return_result(tmp_path):
    now = datetime.now(UTC)
    actor = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("d",))
    path, thread = tmp_path / "checkpoints", str(uuid4())
    with (
        open_protected_checkpointer(path, encryption_key=b"k" * 32, clock=lambda: now) as first,
        open_protected_checkpointer(path, encryption_key=b"k" * 32, clock=lambda: now) as second,
    ):
        first.register_thread(thread, actor, retention_seconds=50)
        with pytest.raises(CheckpointLeaseLostError), first.execution(thread, actor):
            with pytest.raises(CheckpointBusyError), second.execution(thread, actor):
                pass
            stale_context = copy_context()
            now += timedelta(seconds=31)
            with second.execution(thread, actor), pytest.raises(CheckpointLeaseLostError):
                stale_context.run(
                    first.put_writes,
                    {"configurable": {"thread_id": thread}},
                    [("x", "private")],
                    "task",
                )
        with pytest.raises(PermissionError), first.execution(thread, actor):
            now += timedelta(seconds=20)
            assert first.purge_expired() == 0
        assert first.purge_expired() == 1
        with pytest.raises(PermissionError):
            first.put_writes({"configurable": {"thread_id": thread}}, [("x", "private")], "task")
