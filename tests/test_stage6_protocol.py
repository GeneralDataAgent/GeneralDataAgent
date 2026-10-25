"""Paired comparisons hold evaluator/data/budgets fixed while allowing system changes."""

import copy

import pytest

from generaldataagent.evaluation.stage_six import paired_gate
from generaldataagent.evaluation.stage_six_protocol import evaluation_protocol, protocol_hash


def test_protocol_is_reproducible_and_resource_sensitive():
    assert evaluation_protocol(workers=2) == evaluation_protocol(workers=2)
    assert protocol_hash(evaluation_protocol(workers=1)) != protocol_hash(
        evaluation_protocol(workers=2)
    )
    assert "agent_hash" not in evaluation_protocol(workers=2)
    assert "scorer_source_hash" in evaluation_protocol(workers=2)


@pytest.mark.parametrize("deadline", [120, 100, 0, -5])
def test_protocol_refuses_a_deadline_a_budget_timeout_would_exceed(deadline):
    with pytest.raises(ValueError, match="hard case deadline must exceed"):
        evaluation_protocol(workers=1, hard_case_deadline=deadline)


def test_protocol_records_the_declared_deadline_and_changes_the_hash():
    standard = evaluation_protocol(workers=1)
    routed = evaluation_protocol(workers=1, hard_case_deadline=185)
    assert standard["hard_case_deadline_seconds"] == 125
    assert routed["hard_case_deadline_seconds"] == 185
    # The deadline is part of what a reader is asked to trust, so two runs that
    # differ here are not the same protocol.
    assert protocol_hash(standard) != protocol_hash(routed)


@pytest.mark.parametrize("seconds", [0, -1, True, 180.0])
def test_protocol_refuses_an_invalid_declared_time_budget(seconds):
    with pytest.raises(ValueError, match="invalid declared time budget"):
        evaluation_protocol(workers=1, seconds=seconds)


def test_protocol_checks_deadline_against_the_declared_time_budget():
    with pytest.raises(ValueError, match="hard case deadline must exceed"):
        evaluation_protocol(workers=1, seconds=180, hard_case_deadline=180)
    routed = evaluation_protocol(workers=1, seconds=180, hard_case_deadline=185)
    assert routed["seconds"] == 180
    assert protocol_hash(routed) != protocol_hash(
        evaluation_protocol(workers=1, hard_case_deadline=185)
    )
    short = evaluation_protocol(workers=1, seconds=90, hard_case_deadline=95)
    assert short["seconds"] == 90
    assert short["hard_case_deadline_seconds"] == 95


@pytest.mark.parametrize("steps", [0, -1, True, 6.0])
def test_protocol_refuses_an_invalid_declared_step_budget(steps):
    with pytest.raises(ValueError, match="invalid declared step budget"):
        evaluation_protocol(workers=1, steps=steps)


def test_declared_step_budget_is_recorded_and_changes_the_hash():
    standard = evaluation_protocol(workers=1)
    routed = evaluation_protocol(workers=1, steps=8)
    assert standard["steps"] == 6
    assert routed["steps"] == 8
    assert protocol_hash(standard) != protocol_hash(routed)


def test_paired_allows_subject_change_and_blocks_data_or_duplicates():
    before = {
        "cohort_hash": "c",
        "protocol_hash": "p",
        "benchmark": "bird",
        "source_manifest_hash": "s",
        "system_fingerprint": {"tool_profile": "sql"},
        "results": [{"case_id": "1", "passed": True}],
    }
    after = copy.deepcopy(before)
    after["system_fingerprint"] = {"tool_profile": "sql-analysis"}
    assert paired_gate(before, after)["passed"]
    other = copy.deepcopy(after)
    other["source_manifest_hash"] = "changed"
    with pytest.raises(ValueError):
        paired_gate(before, other)
    other = copy.deepcopy(after)
    other["results"] *= 2
    with pytest.raises(ValueError):
        paired_gate(before, other)
    with pytest.raises(ValueError):
        paired_gate(before, after, max_regressions=-1)


def test_scalar_helpers_are_part_of_the_scorer_fingerprint(monkeypatch):
    import generaldataagent.evaluation.stage_six_protocol as module

    original = protocol_hash(module.evaluation_protocol(workers=1))

    def other_number(value):
        return 0

    monkeypatch.setattr(module, "_number", other_number)
    assert protocol_hash(module.evaluation_protocol(workers=1)) != original


def test_per_call_output_budget_is_an_evaluator_invariant():
    normal = evaluation_protocol(workers=1)
    constrained = evaluation_protocol(workers=1, max_output_tokens=1024)
    assert normal["max_output_tokens_per_call"] is None
    assert constrained["max_output_tokens_per_call"] == 1024
    assert protocol_hash(normal) != protocol_hash(constrained)
    with pytest.raises(ValueError):
        evaluation_protocol(workers=1, max_output_tokens=0)


def test_total_token_budget_is_recorded_and_must_match_in_a_pair():
    original = evaluation_protocol(workers=1, max_tokens=24000)
    expanded = evaluation_protocol(workers=1, max_tokens=48000)
    assert original["tokens"] == 24000
    assert expanded["tokens"] == 48000
    assert {k: v for k, v in expanded.items() if k != "tokens"} == {
        k: v for k, v in original.items() if k != "tokens"
    }
    before = {
        "cohort_hash": "c",
        "protocol_hash": protocol_hash(original),
        "benchmark": "infi",
        "results": [{"case_id": "1", "passed": True}],
    }
    after = before | {"protocol_hash": protocol_hash(expanded)}
    with pytest.raises(ValueError):
        paired_gate(before, after)
    for value in (0, -1, True, 48000.0):
        with pytest.raises(ValueError):
            evaluation_protocol(workers=1, max_tokens=value)


def test_default_evaluation_has_no_token_caps_but_still_records_consumption_policy():
    protocol = evaluation_protocol(workers=1)
    assert protocol["tokens"] is None
    assert protocol["max_output_tokens_per_call"] is None
    assert protocol["permitted_effects"] == "read-only"
    assert protocol_hash(protocol) != protocol_hash(
        evaluation_protocol(workers=1, max_tokens=48000)
    )
    assert protocol_hash(protocol) != protocol_hash(
        evaluation_protocol(workers=1, max_output_tokens=3000)
    )
