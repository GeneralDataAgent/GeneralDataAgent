"""Pinned configuration reaches the worker without freezing admission revocations."""

import copy
import io
import json
import sqlite3
import subprocess
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_bounded_agent_skill import ACTOR, extension
from test_bounded_agent_skill import database as database
from test_stage6_evaluation import Model, case
from test_stage10_routing_fingerprint import run_manifest as run_manifest

from generaldataagent.business.governance import CapabilityRegistry
from generaldataagent.evaluation import stage_six_worker as worker
from generaldataagent.evaluation.skill_treatment import SkillTreatment, load_skill_treatment
from generaldataagent.evaluation.stage_six import run_isolated

ROOT = Path(__file__).resolve().parents[1]


def configuration(tmp_path, *, dependencies=True, **changes):
    value = extension(tmp_path, dependencies=dependencies, **changes)
    (tmp_path / "skill.json").write_text(value.skill.model_dump_json())
    entries = []
    for i, skill in enumerate(value.dependencies.values()):
        name = f"dependency-{i}.json"
        (tmp_path / name).write_text(skill.model_dump_json())
        entries.append({"path": name, "skill_hash": value.admission.skill_hash(skill)})
    config = {
        "registry_path": "admission.sqlite",
        "actor": ACTOR.model_dump(mode="json"),
        "skill_path": "skill.json",
        "skill_hash": value.admission.skill_hash(value.skill),
        "dependencies": entries,
    }
    path = tmp_path / "skill-config.json"
    path.write_text(json.dumps(config))
    return path, config, value


def test_snapshot_freezes_definitions_but_observes_live_read_only_admission(tmp_path):
    path, _, value = configuration(tmp_path)
    before = value.admission.registry.path.read_bytes()
    treatment = load_skill_treatment(path, root=tmp_path)
    assert value.admission.registry.path.read_bytes() == before
    exported = treatment.payload()
    exported["body"]["skill"]["prompt_template"] = "Changed"
    (tmp_path / "skill.json").write_text("not valid anymore")
    port = treatment.preflight(profile="sql", task=case().task)
    assert port.skill == value.skill
    assert treatment.summary()["admission_mode"] == "live_read_only_registry"
    assert treatment.payload()["body"]["skill"]["prompt_template"] != "Changed"
    with pytest.raises(sqlite3.OperationalError):
        port.admission.registry.revoke(value.skill.name, value.skill.version)
    assert value.admission.registry.path.read_bytes() == before
    value.admission.registry.revoke("citation-policy", "v1")
    with pytest.raises(PermissionError, match="capability_not_admitted"):
        treatment.preflight()


def test_read_only_registry_never_creates_or_repairs_files(tmp_path):
    absent = tmp_path / "missing" / "registry.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        CapabilityRegistry(absent, clock=lambda: datetime.now(UTC), read_only=True)
    assert not absent.parent.exists()
    empty = tmp_path / "empty.sqlite"
    with sqlite3.connect(empty):
        pass
    before = empty.read_bytes()
    with pytest.raises(sqlite3.OperationalError):
        CapabilityRegistry(empty, clock=lambda: datetime.now(UTC), read_only=True)
    assert empty.read_bytes() == before
    with pytest.raises(ValueError, match="must be boolean"):
        CapabilityRegistry(empty, clock=lambda: None, read_only=1)


@pytest.mark.parametrize(
    "change",
    [
        "pin",
        "outside",
        "unknown",
        "null",
        "duplicate",
        "count",
        "size",
        "actor",
        "missing_registry",
        "revoked",
    ],
)
def test_bad_configuration_is_refused_without_modifying_registry(tmp_path, change):
    path, config, value = configuration(tmp_path)
    if change == "pin":
        config["skill_hash"] = "sha256:" + "0" * 64
    elif change == "outside":
        config["skill_path"] = "../skill.json"
    elif change == "unknown":
        config["extra"] = True
    elif change == "null":
        config["dependencies"] = None
    elif change == "duplicate":
        config["dependencies"] *= 2
    elif change == "count":
        config["dependencies"] *= 65
    elif change == "size":
        (tmp_path / "skill.json").write_bytes(b" " * 128_001)
    elif change == "actor":
        config["actor"]["tenant_id"] = "foreign"
    elif change == "missing_registry":
        config["registry_path"] = "absent.sqlite"
    elif change == "revoked":
        value.admission.registry.revoke("citation-policy", "v1")
    before = value.admission.registry.path.read_bytes()
    path.write_text(json.dumps(config))
    with pytest.raises((ValueError, PermissionError, sqlite3.Error)):
        load_skill_treatment(path, root=tmp_path)
    assert value.admission.registry.path.read_bytes() == before
    assert not (tmp_path / "absent.sqlite").exists()


@pytest.mark.parametrize("change", ["body", "version", "extra", "null"])
def test_invalid_payload_never_starts_a_subprocess(tmp_path, monkeypatch, change):
    path, _, _ = configuration(tmp_path)
    payload = load_skill_treatment(path, root=tmp_path).payload()
    if change == "body":
        payload["body"]["skill"]["prompt_template"] = "Changed"
    elif change == "version":
        payload["version"] = "unknown"
    elif change == "extra":
        payload["extra"] = True
    else:
        payload = None
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: seen.append(a))
    if change == "null":
        with pytest.raises(ValueError):
            SkillTreatment.from_payload(None)
    else:
        with pytest.raises(ValueError):
            run_isolated(
                case().task,
                tmp_path / "input.sqlite",
                app_type="scripted",
                provider_id="fixture",
                skill_treatment=payload,
            )
    assert seen == []


@pytest.mark.parametrize("failure", ["hash", "admission", "budget"])
def test_worker_refuses_before_resolving_or_building_a_provider(tmp_path, monkeypatch, failure):
    path, _, value = configuration(tmp_path)
    treatment = load_skill_treatment(path, root=tmp_path)
    data = treatment.payload()
    if failure == "hash":
        data["body"]["actor"]["user_id"] = "changed"
    elif failure == "admission":
        value.admission.registry.revoke("citation-policy", "v1")
    payload = {
        "skill_treatment": data,
        "task": {"question": "Mean?", "instructions": "@mean[value]"},
        "documents": "",
        "max_steps": 1 if failure == "budget" else 6,
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(
        worker, "resolve_evaluation_model", lambda **kw: pytest.fail("provider was resolved")
    )
    monkeypatch.setattr(worker, "build_evaluation_model", lambda *a: pytest.fail("model was built"))
    with pytest.raises((ValueError, PermissionError)):
        worker.main()


def test_real_subprocess_worker_executes_pinned_skill_and_live_revocation(
    database, tmp_path, monkeypatch
):
    path, _, value = configuration(tmp_path)
    treatment = load_skill_treatment(path, root=tmp_path)
    marker = tmp_path / "provider-built.txt"
    bootstrap = f"""
import sys
from types import SimpleNamespace
sys.path.insert(0, {str(ROOT / "tests")!r})
from test_stage6_evaluation import Model
from generaldataagent.evaluation import stage_six_worker as worker
from pathlib import Path
def resolve(**kwargs):
    with Path({str(marker)!r}).open('a') as stream: stream.write('resolved\\n')
    return SimpleNamespace(), {{'route_hash':'fixture'}}
worker.resolve_evaluation_model = resolve
worker.build_evaluation_model = lambda *args: Model(
    {{'sql':'SELECT AVG(value) AS mean FROM data'}},
    {{'answers':{{'mean':'2'}},'evidence_steps':[1]}}
)
worker.main()
"""
    real_run = subprocess.run

    def execute(command, **kwargs):
        assert "SECRET_REFERENCE_VALUE" not in kwargs["input"]
        return real_run([sys.executable, "-c", bootstrap], **kwargs)

    monkeypatch.setattr(subprocess, "run", execute)
    result = run_isolated(
        case("SECRET_REFERENCE_VALUE").task,
        database,
        app_type="scripted",
        provider_id="fixture",
        skill_treatment=treatment.payload(),
    )
    assert result["failure"] is None and result["answers"] == {"mean": "2"}
    assert result["skill_extension"]["model_responses"] == 2
    assert (
        result["skill_treatment"]["configuration_hash"] == treatment.summary()["configuration_hash"]
    )
    assert marker.read_text().splitlines() == ["resolved"]
    value.admission.registry.revoke("citation-policy", "v1")
    denied = run_isolated(
        case().task,
        database,
        app_type="scripted",
        provider_id="fixture",
        skill_treatment=treatment.payload(),
    )
    assert denied["failure"] == "worker_error" and denied["answers"] == {}
    assert denied["skill_treatment"]["delivery_status"] == "unknown_worker_failure"
    assert "model_responses" not in denied["skill_treatment"]
    assert marker.read_text().splitlines() == ["resolved"]


def setup_runner(run_manifest, tmp_path, monkeypatch):
    runner, run = run_manifest
    (tmp_path / "src").symlink_to(ROOT / "src", target_is_directory=True)
    (tmp_path / "uv.lock").symlink_to(ROOT / "uv.lock")
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner.subprocess, "check_output", lambda *a, **kw: "synthetic-commit\n")
    return runner, run


@pytest.mark.parametrize("change", ["missing", "wrong_hash", "missing_instruction_metadata"])
def test_worker_cannot_silently_drop_or_replace_the_treatment(tmp_path, monkeypatch, change):
    from types import SimpleNamespace

    path, _, _ = configuration(tmp_path)
    treatment = load_skill_treatment(path, root=tmp_path)
    result = {
        "elapsed_seconds": 0,
        "answers": {"mean": "2"},
        "failure": None,
        "skill_treatment": treatment.summary(),
        "skill_extension": {"mode": "admitted_instructions"},
    }
    if change == "missing":
        result.pop("skill_treatment")
    elif change == "wrong_hash":
        result["skill_treatment"]["configuration_hash"] = "sha256:" + "0" * 64
    else:
        result.pop("skill_extension")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=json.dumps(result))
    )
    observed = run_isolated(
        case().task,
        tmp_path / "input.sqlite",
        app_type="fixture",
        provider_id="fixture",
        skill_treatment=treatment.payload(),
    )
    assert observed["failure"] == "worker_error" and observed["answers"] == {}
    assert observed["skill_treatment"]["delivery_status"] == "unknown_worker_failure"


def test_cli_protocol_snapshot_and_worker_payload_match(run_manifest, tmp_path, monkeypatch):
    runner, run = setup_runner(run_manifest, tmp_path, monkeypatch)
    path, _, _ = configuration(tmp_path)
    execute = runner.run_isolated
    received = []

    def capture(*args, **kwargs):
        received.append(copy.deepcopy(kwargs.get("skill_treatment")))
        return execute(*args, **kwargs)

    monkeypatch.setattr(runner, "run_isolated", capture)
    enabled, _ = run(["Count payments"], routing=False, skill_treatment=path)
    off, _ = run(["Count payments"], routing=False)
    assert received[-1] is None
    assert "skill_treatment" not in off and "skill_treatment" not in off["protocol"]
    assert enabled["protocol_hash"] != off["protocol_hash"]
    frozen = json.loads((tmp_path / "run-1" / "skill-treatment.json").read_text())
    assert received[0] == frozen
    assert enabled["protocol"]["skill_treatment"] == SkillTreatment.from_payload(frozen).summary()
    assert enabled["skill_treatment"] == enabled["protocol"]["skill_treatment"]
    assert enabled["system_fingerprint"]["skill_treatment"]["source_hashes"][
        "evaluation/skill_treatment.py"
    ] == runner.digest(ROOT / "src/generaldataagent/evaluation/skill_treatment.py")
    assert (
        enabled["system_fingerprint"]["skill_treatment"]["configuration_hash"]
        == frozen["content_hash"]
    )


def test_routed_skill_budget_must_fit_every_actual_lane(run_manifest, tmp_path, monkeypatch):
    runner, run = setup_runner(run_manifest, tmp_path, monkeypatch)
    path, _, _ = configuration(tmp_path)
    calls = []
    monkeypatch.setattr(runner, "run_isolated", lambda *a, **kw: calls.append(kw))
    with pytest.raises(SystemExit):
        run(["How many payments?"], skill_treatment=path)
    assert calls == []


def test_compatible_treatment_reaches_all_frozen_routing_lanes(run_manifest, tmp_path, monkeypatch):
    runner, run = setup_runner(run_manifest, tmp_path, monkeypatch)
    path, _, _ = configuration(
        tmp_path,
        dependencies=False,
        budget={"max_tool_calls": 5, "max_tokens": 1000, "max_seconds": 30},
    )
    execute = runner.run_isolated
    seen = []

    def capture(*args, **kwargs):
        seen.append((kwargs["max_steps"], kwargs["skill_treatment"]))
        return execute(*args, **kwargs)

    monkeypatch.setattr(runner, "run_isolated", capture)
    report, profiles = run(
        ["How many payments?", "Find the median amount", "What is the fee?"],
        skill_treatment=path,
    )
    assert len(set(profiles)) == 3 and sorted(steps for steps, _ in seen) == [5, 7, 8]
    assert all(
        payload["content_hash"] == report["protocol"]["skill_treatment"]["configuration_hash"]
        for _, payload in seen
    )


def test_cli_reaches_real_subprocess_sql_and_retains_treatment_evidence(
    run_manifest, tmp_path, monkeypatch
):
    runner, run = setup_runner(run_manifest, tmp_path, monkeypatch)
    path, _, _ = configuration(tmp_path)
    monkeypatch.setattr(runner, "run_isolated", run_isolated)
    bootstrap = f"""
import sys
from types import SimpleNamespace
sys.path.insert(0, {str(ROOT / "tests")!r})
from test_stage6_evaluation import Model
from generaldataagent.evaluation import stage_six_worker as worker
worker.resolve_evaluation_model = lambda **kwargs: (SimpleNamespace(), {{'route_hash':'scripted'}})
worker.build_evaluation_model = lambda *args: Model(
    {{'sql':'SELECT COUNT(*) AS answer FROM payments'}},
    {{'answers':{{'answer':'1'}},'evidence_steps':[1]}}
)
worker.main()
"""
    real_run = subprocess.run
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: real_run([sys.executable, "-c", bootstrap], **kwargs),
    )
    report, _ = run(["Count payments"], routing=False, skill_treatment=path)
    row = report["results"][0]
    assert row["passed"] is True and row["answers"] == {"answer": "1"}
    assert row["skill_extension"]["model_responses"] == 2
    assert len(row["skill_extension"]["references"]) == 2
    assert row["skill_treatment"] == report["skill_treatment"]
    assert row["skill_treatment"] == report["protocol"]["skill_treatment"]
    assert report["production_accepted"] is False


def test_read_only_worker_observes_revocation_during_model_and_keeps_usage(
    database, tmp_path, monkeypatch, capsys
):
    path, _, value = configuration(tmp_path)
    treatment = load_skill_treatment(path, root=tmp_path)
    payload = {
        "task": asdict(case().task),
        "database": str(database),
        "documents": "",
        "skill_treatment": treatment.payload(),
        "app_type": "fixture",
        "provider_id": "fixture",
    }

    class RevokeModel(Model):
        def invoke(self, messages):
            response = super().invoke(messages)
            value.admission.registry.revoke("citation-policy", "v1")
            return response

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(worker, "resolve_evaluation_model", lambda **kw: (object(), {}))
    monkeypatch.setattr(
        worker,
        "build_evaluation_model",
        lambda *a: RevokeModel({"sql": "SELECT AVG(value) AS mean FROM data"}),
    )
    worker.main()
    result = json.loads(capsys.readouterr().out)
    assert result["failure"] == "skill_admission_refused" and result["answers"] == {}
    assert result["tokens"] == 10 and result["skill_extension"]["model_responses"] == 1
