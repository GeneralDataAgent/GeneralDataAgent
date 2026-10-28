"""A cohort is unseen only if it was declared before the run that uses it."""

import json

import pytest

from generaldataagent.evaluation.frozen_cohort import (
    freeze_cohort,
    load_frozen_cohort,
    select_frozen_cases,
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
