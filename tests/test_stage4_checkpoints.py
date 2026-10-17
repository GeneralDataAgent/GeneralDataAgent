from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from generaldataagent.contracts import ActorContext
from generaldataagent.orchestration import PhaseOneServices, build_phase_one_graph
from generaldataagent.orchestration.checkpoints import open_protected_checkpointer


def test_100_interrupted_workflows_resume_after_restart_without_replaying_completed_analysis(
    tmp_path,
):
    path = tmp_path / "checkpoints"
    key = b"k" * 32
    actor = ActorContext(
        user_id="private-user", tenant_id="private-tenant", project_id="p", data_domains=("d",)
    )
    counts = {}

    def analyze(state):
        counts[state["task_id"]] = counts.get(state["task_id"], 0) + 1
        return {"analysis_result": {"private_result": "private-customer-result"}}

    services = PhaseOneServices(
        discover=lambda _: {"semantic_context": {}},
        plan=lambda _: {"task_plan": {}},
        analyze=analyze,
        validate=lambda _: {"validation_result": {"status": "pass"}},
        deliver=lambda _: {"status": "completed"},
    )
    for index in range(100):
        thread = str(uuid4())
        config = {"configurable": {"thread_id": thread}}
        with open_protected_checkpointer(path, encryption_key=key) as saver:
            saver.register_thread(thread, actor)
            graph = build_phase_one_graph(services, checkpointer=saver)
            graph.invoke(
                {
                    "task_id": thread,
                    "actor_context": actor.model_dump(mode="json"),
                    "user_query": "private-user-question",
                },
                config,
                interrupt_after=[("discover", "plan", "analyze", "validate")[index % 4]],
            )
            assert graph.get_state(config).next
        with open_protected_checkpointer(path, encryption_key=key) as restored:
            assert restored.authorize_thread(thread, actor) == actor
            graph = build_phase_one_graph(services, checkpointer=restored)
            assert graph.invoke(None, config)["status"] == "completed"
            assert counts[thread] == 1
    # Inspect DB and WAL bytes after close: neither results nor envelope scope is plaintext.
    raw = b"".join(file.read_bytes() for file in tmp_path.iterdir() if file.is_file())
    for sensitive in (b"private-user-question", b"private-customer-result", b"private-tenant"):
        assert sensitive not in raw


def test_encrypted_checkpoint_scope_retention_hold_wrong_key_and_plaintext_rejection(tmp_path):
    path = tmp_path / "checkpoints"
    now = datetime.now(UTC)
    actor = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("d",))
    thread = str(uuid4())
    with open_protected_checkpointer(path, encryption_key=b"k" * 32, clock=lambda: now) as saver:
        saver.register_thread(thread, actor, retention_seconds=1)
        assert (
            saver.authorize_thread(
                thread, actor.model_copy(update={"data_domains": ("d", "extra")})
            )
            == actor
        )
        for change in (
            {"tenant_id": "other"},
            {"user_id": "other"},
            {"project_id": "other"},
            {"data_domains": ("other",)},
        ):
            with pytest.raises(PermissionError):
                saver.authorize_thread(thread, actor.model_copy(update=change))
        with pytest.raises(PermissionError):
            saver.register_thread(thread, actor)
        with pytest.raises(ValueError, match="plaintext"):
            saver.serde.loads_typed(("json", b"{}"))
        saver.hold_thread(thread, held=True)
        now += timedelta(seconds=1)
        with pytest.raises(PermissionError):
            saver.authorize_thread(thread, actor)
        assert saver.purge_expired() == 0
        saver.hold_thread(thread, held=False)
        assert saver.purge_expired() == 1
        assert saver.purge_expired() == 0
    with (
        pytest.raises(ValueError),
        open_protected_checkpointer(tmp_path / "bad", encryption_key=b"short"),
    ):
        pass
