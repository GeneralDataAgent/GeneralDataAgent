"""The declared resource limits and the actual process envelope stay aligned."""

import json
import subprocess
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from generaldataagent.evaluation.execution_budget import ExecutionBudget
from generaldataagent.evaluation.stage_six import Task, run_isolated
from generaldataagent.evaluation.stage_six_protocol import evaluation_protocol


@pytest.mark.parametrize(
    "steps,seconds,repairs", [(6, 120, 3), (5, 90, 2), (7, 150, 3), (8, 180, 3)]
)
@pytest.mark.parametrize("tokens,output", [(None, None), (24000, 3000)])
def test_protocol_payload_and_process_timeout_share_one_value(
    tmp_path, monkeypatch, steps, seconds, repairs, tokens, output
):
    budget = ExecutionBudget(
        max_steps=steps,
        max_seconds=seconds,
        max_repairs=repairs,
        max_tokens=tokens,
        max_output_tokens=output,
    )
    protocol = evaluation_protocol(workers=1, **budget.protocol_kwargs())
    seen = {}

    def execute(*args, **kwargs):
        seen.update(timeout=kwargs["timeout"], payload=json.loads(kwargs["input"]))
        return SimpleNamespace(stdout=json.dumps({"elapsed_seconds": 0, "failure": None}))

    monkeypatch.setattr(subprocess, "run", execute)
    run_isolated(
        Task("synthetic", "budget", "Count rows", "", "easy", "unused"),
        tmp_path / "data.sqlite",
        app_type="scripted",
        provider_id="scripted",
        **budget.worker_kwargs(),
    )
    payload = seen["payload"]
    assert protocol["steps"] == payload["max_steps"] == steps
    assert protocol["seconds"] == payload["max_seconds"] == seconds
    assert protocol["tokens"] == payload["max_tokens"] == tokens
    assert protocol["max_output_tokens_per_call"] == payload["max_output_tokens"] == output
    assert payload["max_repairs"] == repairs
    assert protocol["hard_case_deadline_seconds"] == seen["timeout"] == seconds + 5


def test_budget_and_exported_values_cannot_mutate_one_another():
    budget = ExecutionBudget()
    with pytest.raises(FrozenInstanceError):
        budget.max_steps = 100
    payload = budget.worker_payload()
    payload["max_steps"] = 999
    assert budget.max_steps == 6
    assert budget.worker_kwargs()["max_steps"] == 6


@pytest.mark.parametrize(
    "change",
    [
        {"max_steps": False},
        {"max_steps": 0},
        {"max_repairs": -1},
        {"max_repairs": 2.0},
        {"max_output_tokens": 31},
        {"max_tokens": True},
        {"max_seconds": float("nan")},
        {"deadline_seconds": float("inf")},
        {"deadline_seconds": 120},
    ],
)
def test_invalid_budget_never_starts_a_worker(tmp_path, monkeypatch, change):
    def forbidden(*args, **kwargs):
        pytest.fail("worker was started")

    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(ValueError):
        run_isolated(
            Task("synthetic", "invalid", "Count rows", "", "easy", "unused"),
            tmp_path / "data.sqlite",
            app_type="scripted",
            provider_id="scripted",
            **change,
        )


@pytest.mark.parametrize("index", [0, 1, 2])
def test_legacy_protocol_fields_and_hashes_remain_identical(index):
    # Checked-in pre-refactor oracle also works in shallow CI checkouts.
    import platform
    import sqlite3
    from pathlib import Path

    from generaldataagent.evaluation.stage_six_protocol import protocol_hash

    fixture = json.loads(
        (Path(__file__).parent / "fixtures/stage6-protocol-507aa12.json").read_text()
    )
    variant = fixture["variants"][index]
    old = variant["protocol"]
    old["python_runtime"] = platform.python_version()
    old["sqlite_runtime"] = sqlite3.sqlite_version
    current = evaluation_protocol(workers=1, **variant["arguments"])
    assert current == old
    assert protocol_hash(current) == protocol_hash(old)
