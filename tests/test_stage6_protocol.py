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
    assert normal["max_output_tokens_per_call"] == 3000
    assert constrained["max_output_tokens_per_call"] == 1024
    assert protocol_hash(normal) != protocol_hash(constrained)
    with pytest.raises(ValueError):
        evaluation_protocol(workers=1, max_output_tokens=0)


def test_total_token_budget_is_recorded_and_must_match_in_a_pair():
    original = evaluation_protocol(workers=1)
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
