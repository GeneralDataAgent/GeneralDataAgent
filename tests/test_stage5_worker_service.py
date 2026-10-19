import signal
from types import SimpleNamespace

import pytest
from test_stage5_worker import prepare
from test_stage5_worker import setup as action_fixture

from generaldataagent.services import report_worker as service


@pytest.fixture
def worker(tmp_path):
    setup = action_fixture.__wrapped__(tmp_path)
    return prepare(tmp_path, setup)[0]


def test_operator_factory_is_fixed_typed_and_no_user_session_service(worker, monkeypatch):
    monkeypatch.setattr(
        service,
        "importlib",
        SimpleNamespace(import_module=lambda name: SimpleNamespace(create=lambda: worker)),
    )
    assert service.load_worker("operator_runtime:create", development=True) is worker
    with pytest.raises(ValueError):
        service.load_worker("operator_runtime:create")
    for invalid in [
        "module:create()",
        "module:/tmp/code",
        "../../module:create",
        "module:Class.method",
    ]:
        with pytest.raises(ValueError):
            service.load_worker(invalid, development=True)
    monkeypatch.setattr(
        service,
        "importlib",
        SimpleNamespace(import_module=lambda _: SimpleNamespace(create=lambda: object())),
    )
    with pytest.raises(ValueError):
        service.load_worker("operator_runtime:create", development=True)


def test_once_poll_keeps_approvals_and_sanitizes_process_logs(worker, capsys):
    service.run_worker(worker, once=True)
    result = capsys.readouterr().out
    assert "poll_complete" in result and '"jobs": 1' in result
    with worker.scheduler.actions._db() as db:
        assert db.execute("SELECT state FROM business_actions").fetchone()[0] == "pending"
    service.summarize([{"state": "failed", "error": "token: sensitive", "schedule_id": "private"}])
    result = capsys.readouterr().out
    assert "sensitive" not in result and "private" not in result
    with pytest.raises(ValueError):
        service.summarize([{"state": "unexpected"}])


def test_sigterm_graceful_stop_and_handler_restoration(worker, monkeypatch):
    handlers = {}
    registered = []

    def install(name, callback):
        registered.append(name)
        previous = handlers.get(name, "original")
        handlers[name] = callback
        return previous

    monkeypatch.setattr(service.signal, "signal", install)

    def run(stop, on_result):
        assert not stop.is_set()
        handlers[signal.SIGTERM]()
        assert stop.is_set()

    monkeypatch.setattr(worker, "run", run)
    service.run_worker(worker)
    assert registered == [signal.SIGINT, signal.SIGTERM, signal.SIGINT, signal.SIGTERM]
    assert handlers[signal.SIGTERM] == "original"


def test_entrypoint_check_only_and_sanitized_failure(worker, monkeypatch, capsys):
    monkeypatch.setattr(
        service,
        "importlib",
        SimpleNamespace(import_module=lambda _: SimpleNamespace(create=lambda: worker)),
    )
    assert (
        service.main(["--factory", "operator_runtime:create", "--development", "--check-config"])
        == 0
    )
    assert "configured" in capsys.readouterr().out
    with worker.scheduler.actions._db() as db:
        assert db.execute("SELECT count(*) FROM business_actions").fetchone()[0] == 0
    assert service.main(["--factory", "operator_runtime:create"]) == 1
    assert capsys.readouterr().err == '{"state":"failed","error":"worker_service_failed"}\n'


def test_real_signed_workload_factory_runs_once_without_delivery(tmp_path, monkeypatch, capsys):
    from test_stage5_workload_identity import prepare as prepare_workload

    from generaldataagent.security.identity import SessionBroker

    worker, *_ = prepare_workload(tmp_path)
    monkeypatch.setattr(
        service,
        "importlib",
        SimpleNamespace(import_module=lambda _: SimpleNamespace(create=lambda: worker)),
    )
    assert service.load_worker("operator_runtime:create") is worker
    assert service.main(["--factory", "operator_runtime:create", "--once"]) == 0
    assert "poll_complete" in capsys.readouterr().out
    with worker.scheduler.actions._db() as db:
        assert db.execute("SELECT state FROM business_actions").fetchone()[0] == "pending"
    worker.token_provider.broker = SessionBroker()
    with pytest.raises(ValueError):
        service.load_worker("operator_runtime:create")


def test_one_shot_failure_has_nonzero_exit_without_sensitive_output(worker, monkeypatch, capsys):
    monkeypatch.setattr(worker, "run_once", lambda: [{"state": "failed", "error": "secret"}])
    assert service.run_worker(worker, once=True) == 1
    assert "secret" not in capsys.readouterr().out
