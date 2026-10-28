"""A cohort is unseen only if it was declared before the run that uses it."""

import json

import pytest

from generaldataagent.evaluation.frozen_cohort import (
    cohort_drift,
    freeze_cohort,
    load_frozen_cohort,
    select_frozen_cases,
    verify_cohort_file,
)
from generaldataagent.evaluation.stage_six import Case, Task, cohort_hash

BENCHMARK = "bird-mini-dev-sqlite"
CASES = [
    Case(Task(BENCHMARK, f"q{i}", f"question {i}", "", "easy", "asset"), {"answer": "1"})
    for i in range(10)
]


def case(case_id, *, question=None):
    return Case(
        Task(BENCHMARK, case_id, question or f"question {case_id}", "", "easy", "asset"),
        {"answer": "1"},
    )


def report(ids, *, cohort="h1"):
    return {
        "benchmark": BENCHMARK,
        "cohort_hash": cohort,
        "results": [{"case_id": case_id} for case_id in ids],
    }


def freeze(limit=6, seed=1, seen=("q0", "q1", "q2", "q3")):
    return freeze_cohort(CASES, benchmark=BENCHMARK, limit=limit, seed=seed, reports=[report(seen)])


def test_the_declaration_excludes_every_case_a_prior_report_ran():
    cohort = freeze()
    assert cohort["excluded_seen"] == 4
    assert cohort["available_unseen"] == 6
    assert set(cohort["case_ids"]).isdisjoint({"q0", "q1", "q2", "q3"})
    assert cohort["cohort_hash"] == cohort_hash(select_frozen_cases(CASES, cohort))


def test_the_declaration_is_deterministic_and_stores_no_task_text():
    first = freeze(limit=3, seed=7, seen=("q0",))
    second = freeze(limit=3, seed=7, seen=("q0",))
    assert first == second
    serialized = json.dumps(first)
    assert all(item.task.question not in serialized for item in CASES)


def test_the_declared_order_is_the_run_order():
    cohort = freeze(limit=4, seed=3)
    selected = select_frozen_cases(CASES, cohort)
    assert [item.task.case_id for item in selected] == cohort["case_ids"]
    assert cohort_hash(selected) == cohort["cohort_hash"]


def test_it_refuses_to_declare_more_than_remains():
    with pytest.raises(ValueError, match="unseen"):
        freeze(limit=8, seen=tuple(f"q{i}" for i in range(5)))


def test_a_prior_report_from_another_benchmark_is_refused():
    other = {"benchmark": "dabstep-dev", "cohort_hash": "x", "results": [{"case_id": "q0"}]}
    with pytest.raises(ValueError, match="another benchmark"):
        freeze_cohort(CASES, benchmark=BENCHMARK, limit=1, seed=1, reports=[other])


def test_a_prior_report_is_required_to_know_what_is_used():
    with pytest.raises(ValueError, match="prior report"):
        freeze_cohort(CASES, benchmark=BENCHMARK, limit=1, seed=1, reports=[])


def test_the_runner_refuses_a_task_that_changed_since_the_freeze():
    cohort = freeze(limit=3, seed=1)
    frozen_id = cohort["case_ids"][0]
    changed = [
        case(item.task.case_id, question="edited") if item.task.case_id == frozen_id else item
        for item in CASES
    ]
    with pytest.raises(ValueError, match="does not match"):
        select_frozen_cases(changed, cohort)


@pytest.mark.parametrize(
    "tamper",
    [
        lambda c: {**c, "case_ids": c["case_ids"] + c["case_ids"][:1]},
        lambda c: {**c, "case_ids": c["case_ids"] + ["absent"]},
        lambda c: {k: v for k, v in c.items() if k != "cohort_hash"},
        lambda c: {**c, "benchmark": "dabstep-dev"},
    ],
)
def test_a_tampered_file_is_refused(tamper):
    with pytest.raises(ValueError):
        select_frozen_cases(CASES, tamper(freeze(limit=3, seed=1)))


def test_a_file_that_is_not_an_object_is_refused(tmp_path):
    path = tmp_path / "cohort.json"
    path.write_text("[1, 2]")
    with pytest.raises(ValueError, match="JSON object"):
        load_frozen_cohort(path)
    path.write_text("not json")
    with pytest.raises(ValueError, match="valid JSON"):
        load_frozen_cohort(path)


def test_the_declaration_records_a_hash_per_case():
    cohort = freeze(limit=3, seed=1)
    assert sorted(cohort["case_hashes"]) == sorted(cohort["case_ids"])
    assert len(set(cohort["case_hashes"].values())) == 3


def test_a_changed_task_is_named_instead_of_only_counted():
    cohort = freeze(limit=3, seed=1)
    frozen_id = cohort["case_ids"][0]
    changed = [
        case(item.task.case_id, question="edited") if item.task.case_id == frozen_id else item
        for item in CASES
    ]
    with pytest.raises(ValueError, match=f"content changed for {frozen_id}$"):
        select_frozen_cases(changed, cohort)
    drift = cohort_drift(changed, cohort)
    assert drift["changed_case_ids"] == [frozen_id]
    assert drift["localized"] is True


def test_a_file_without_per_case_hashes_says_it_cannot_localize():
    cohort = {k: v for k, v in freeze(limit=3, seed=1).items() if k != "case_hashes"}
    changed = [case(item.task.case_id, question="edited") for item in CASES]
    with pytest.raises(ValueError, match="no per-case hashes"):
        select_frozen_cases(changed, cohort)
    assert cohort_drift(changed, cohort)["localized"] is False


def test_the_declared_benchmark_is_named_in_the_refusal():
    cohort = freeze(limit=3, seed=1)
    other = [Case(Task("dabstep-dev", "q0", "question", "", "easy", "asset"), {"answer": "1"})]
    with pytest.raises(ValueError, match="declared for 'bird-mini-dev-sqlite'"):
        select_frozen_cases(other, cohort)


def run_record(**overrides):
    return {
        "benchmark": BENCHMARK,
        "cohort_hash": "h-1",
        "cohort_file_hash": "f-1",
        **overrides,
    }


DECLARATION = {"benchmark": BENCHMARK, "cohort_hash": "h-1", "case_ids": ["q4", "q5"]}


def test_a_run_is_checked_against_the_file_not_just_the_other_arm():
    summary = verify_cohort_file([run_record()], DECLARATION, file_sha256="f-1")
    assert summary == {
        "benchmark": BENCHMARK,
        "cohort_hash": "h-1",
        "file_sha256": "f-1",
        "declared_case_ids": 2,
        "runs": 1,
        "case_hashes_present": False,
    }


def test_a_different_file_is_refused_even_when_the_arms_agree_with_each_other():
    with pytest.raises(ValueError, match="not the file the run declared"):
        verify_cohort_file([run_record()], DECLARATION, file_sha256="f-2")


def test_a_file_from_another_benchmark_is_refused():
    with pytest.raises(ValueError, match="declared for 'bird-mini-dev-sqlite'"):
        verify_cohort_file([run_record(benchmark="dabstep-dev")], DECLARATION, file_sha256="f-1")


def test_a_run_that_covered_another_cohort_is_refused():
    with pytest.raises(ValueError, match="cohort does not match"):
        verify_cohort_file([run_record(cohort_hash="h-2")], DECLARATION, file_sha256="f-1")
