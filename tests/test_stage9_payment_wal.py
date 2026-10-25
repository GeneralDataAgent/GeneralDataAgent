import json
import sqlite3
from dataclasses import replace

import pytest

from generaldataagent.business.rules.payments import SQLitePaymentsSource
from generaldataagent.contracts import ActorContext
from generaldataagent.contracts.rule_tools import RuleAnalysisRequest
from generaldataagent.orchestration.checkpoints import open_protected_checkpointer
from generaldataagent.orchestration.rule_workflow import RuleWorkflowRuntime, RuleWorkflowServices

ACTOR = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("payments",))
REQUEST = RuleAnalysisRequest(operation="select", semantics_excerpt="null means all")


@pytest.fixture
def wal_source(tmp_path):
    path = tmp_path / "payments.sqlite"
    with sqlite3.connect(path) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("CREATE TABLE merchant_data (payload TEXT)")
        writer.execute("CREATE TABLE payments (eur_amount INTEGER)")
        writer.execute("INSERT INTO merchant_data VALUES (?)", (json.dumps({"merchant": "Shop"}),))
        writer.execute("INSERT INTO payments VALUES (100)")
        writer.commit()
        assert writer.execute("PRAGMA wal_checkpoint(RESTART)").fetchone()[0] == 0
        source = SQLitePaymentsSource(path, allowed_relations={"merchant_data", "payments"})
        yield source, writer


def commit_same_size_wal(source, writer):
    path = source.database
    wal = path.with_name(path.name + "-wal")
    before = (path.stat().st_mtime_ns, path.stat().st_size, wal.stat().st_size)
    writer.execute("UPDATE payments SET eur_amount = 200")
    writer.commit()
    after = (path.stat().st_mtime_ns, path.stat().st_size, wal.stat().st_size)
    assert after == before, (
        "fixture must change WAL content without growing it or updating the main DB"
    )


def test_cached_source_hash_detects_committed_same_size_wal_change(wal_source):
    source, writer = wal_source
    before = source.snapshot_hash()
    commit_same_size_wal(source, writer)
    fresh = SQLitePaymentsSource(
        source.database, allowed_relations={"merchant_data", "payments"}
    ).snapshot_hash()
    assert fresh != before
    assert source.snapshot_hash() == fresh


@pytest.mark.parametrize("change_during_node", [False, True])
def test_wal_change_rejects_resume_or_post_node_binding(wal_source, tmp_path, change_during_node):
    source, writer = wal_source
    counts = {}

    def node(name, value):
        def execute(state):
            counts[name] = counts.get(name, 0) + 1
            return value

        return execute

    ports = RuleWorkflowServices(
        monthly_context=node("context", {}),
        match_rules=node("match", {"ids": []}),
        compute=node("compute", {}),
        verify=node("verify", {"passed": True, "failures": []}),
        deliver=node("deliver", {}),
    )
    if change_during_node:

        def change(state):
            commit_same_size_wal(source, writer)
            return {}

        ports = replace(ports, compute=change)
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow = RuleWorkflowRuntime(
            services=ports,
            checkpointer=saver,
            source_hashes=lambda: {"payments": source.snapshot_hash()},
            workflow_version="wal-binding-v1",
        )
        if change_during_node:
            with pytest.raises(ValueError, match="rule_workflow_binding_changed"):
                flow.run(REQUEST, ACTOR)
            assert "verify" not in counts
        else:
            thread, _ = flow.run(REQUEST, ACTOR, interrupt_after=("monthly_context",))
            before = dict(counts)
            commit_same_size_wal(source, writer)
            with pytest.raises(ValueError, match="rule_workflow_binding_changed"):
                flow.resume(thread, REQUEST, ACTOR)
            assert counts == before
        assert "deliver" not in counts
