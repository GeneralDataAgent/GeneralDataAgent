from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from generaldataagent.business.experience import (
    ExperienceError,
    ExperienceRecord,
    ExperienceStore,
    experience_brief,
)
from generaldataagent.contracts import ActorContext

ACTOR = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("payments",))
OTHER_TENANT = ActorContext(
    user_id="u2", tenant_id="t2", project_id="p", data_domains=("payments",)
)
OTHER_DOMAIN = ActorContext(user_id="u3", tenant_id="t", project_id="p", data_domains=("sales",))


def record(**changes):
    values = dict(
        failure_class="rule_context_required",
        applies_when=("a contextual rule operation was attempted without a merchant",),
        does_not_apply_when=("the operation filters only supplied facets",),
        repair="supply merchant and year, then choose exactly one time scope",
        counterexample="adding a month and a day of year together is still refused",
        tool_scope=("rules",),
        owner="u",
        created_at=datetime.now(UTC),
    )
    return ExperienceRecord(**(values | changes))


def store(tmp_path):
    return ExperienceStore(tmp_path / "experience.sqlite", clock=lambda: datetime.now(UTC))


def test_candidate_records_are_invisible_to_runtime_retrieval(tmp_path):
    service = store(tmp_path)
    item = record()
    service.capture(item, ACTOR)
    assert service.state(item.experience_id, ACTOR) == "candidate"
    assert service.retrieve(failure_class="rule_context_required", tool="rules", actor=ACTOR) == ()


def test_only_accepted_independent_review_activates_a_record(tmp_path):
    service = store(tmp_path)
    item = record()
    service.capture(item, ACTOR)
    # The capturing user may not review their own record.
    with pytest.raises(ExperienceError) as denied:
        service.review(item.experience_id, reviewer_id="u", accepted=True, actor=ACTOR)
    assert denied.value.code == "experience_review_denied"
    assert service.retrieve(failure_class="rule_context_required", tool="rules", actor=ACTOR) == ()
    service.review(item.experience_id, reviewer_id="reviewer-1", accepted=True, actor=ACTOR)
    found = service.retrieve(failure_class="rule_context_required", tool="rules", actor=ACTOR)
    assert [row.experience_id for row in found] == [item.experience_id]
    # A rejected review leaves the record inactive.
    second = record()
    service.capture(second, ACTOR)
    service.review(second.experience_id, reviewer_id="reviewer-1", accepted=False, actor=ACTOR)
    assert service.state(second.experience_id, ACTOR) == "candidate"
    assert [
        row.experience_id
        for row in service.retrieve(
            failure_class="rule_context_required", tool="rules", actor=ACTOR
        )
    ] == [item.experience_id]


def test_retrieval_is_scoped_by_failure_class_and_tool(tmp_path):
    service = store(tmp_path)
    item = record()
    service.capture(item, ACTOR)
    service.review(item.experience_id, reviewer_id="reviewer-1", accepted=True, actor=ACTOR)
    assert service.retrieve(failure_class="unknown_column", tool="rules", actor=ACTOR) == ()
    assert service.retrieve(failure_class="rule_context_required", tool="sql", actor=ACTOR) == ()
    with pytest.raises(ValueError):
        service.retrieve(failure_class="  ", tool="rules", actor=ACTOR)
    with pytest.raises(ValueError):
        service.retrieve(failure_class="x", tool="rules", actor=ACTOR, limit=0)


@pytest.mark.parametrize("actor", [OTHER_TENANT, OTHER_DOMAIN])
def test_records_do_not_cross_actor_scope(tmp_path, actor):
    service = store(tmp_path)
    item = record()
    service.capture(item, ACTOR)
    service.review(item.experience_id, reviewer_id="reviewer-1", accepted=True, actor=ACTOR)
    assert service.retrieve(failure_class="rule_context_required", tool="rules", actor=actor) == ()
    with pytest.raises(ExperienceError) as denied:
        service.state(item.experience_id, actor)
    assert denied.value.code == "experience_scope_mismatch"


@pytest.mark.parametrize(
    "field,value",
    [
        ("repair", "run SELECT fee FROM fees and copy the value"),
        ("counterexample", "the expected_answer is 0.064"),
        ("repair", "reference sql is available in the workbook"),
    ],
)
def test_answer_material_is_refused_at_capture(tmp_path, field, value):
    service = store(tmp_path)
    with pytest.raises(ExperienceError) as denied:
        service.capture(record(**{field: value}), ACTOR)
    assert denied.value.code == "experience_contains_answer_material"
    assert service.retrieve(failure_class="rule_context_required", tool="rules", actor=ACTOR) == ()


def test_answer_material_hidden_in_a_condition_is_also_refused(tmp_path):
    service = store(tmp_path)
    with pytest.raises(ExperienceError) as denied:
        service.capture(record(applies_when=("select amount from payments",)), ACTOR)
    assert denied.value.code == "experience_contains_answer_material"


@pytest.mark.parametrize(
    "repair",
    [
        "select exactly one time scope from month or day_of_year",
        "select the merchant from merchant_data before retrying",
        "use select_from semantics instead of a facet-only call",
    ],
)
def test_ordinary_guidance_using_the_word_select_is_not_mistaken_for_sql(tmp_path, repair):
    service = store(tmp_path)
    item = record(repair=repair)
    service.capture(item, ACTOR)
    assert service.state(item.experience_id, ACTOR) == "candidate"


def test_blank_condition_members_are_refused(tmp_path):
    service = store(tmp_path)
    with pytest.raises(ExperienceError) as denied:
        service.capture(record(does_not_apply_when=("   ",)), ACTOR)
    assert denied.value.code == "experience_contains_answer_material"


def test_record_contract_has_no_question_or_answer_field():
    fields = set(ExperienceRecord.model_fields)
    assert fields == {
        "schema_version",
        "experience_id",
        "failure_class",
        "applies_when",
        "does_not_apply_when",
        "repair",
        "counterexample",
        "tool_scope",
        "owner",
        "created_at",
    }
    with pytest.raises(ValidationError):
        record(question="how much fee for shop X?")
    with pytest.raises(ValidationError):
        record(expected_answer="0.064")


def test_capture_is_idempotent_by_id_and_detects_tampering(tmp_path):
    service = store(tmp_path)
    item = record()
    service.capture(item, ACTOR)
    with pytest.raises(ExperienceError) as duplicate:
        service.capture(item, ACTOR)
    assert duplicate.value.code == "experience_already_exists"
    # Rewrite the stored body without updating the hash: retrieval must refuse.
    import sqlite3

    with sqlite3.connect(service.path) as db:
        db.execute(
            "UPDATE experiences SET body=? WHERE id=?",
            (record(repair="a different lesson").model_dump_json(), str(item.experience_id)),
        )
        db.execute("UPDATE experiences SET state='active' WHERE id=?", (str(item.experience_id),))
    with pytest.raises(ExperienceError) as tampered:
        service.retrieve(failure_class="rule_context_required", tool="rules", actor=ACTOR)
    assert tampered.value.code == "experience_integrity_failure"


def test_brief_exposes_lessons_and_never_the_counterexample_text(tmp_path):
    service = store(tmp_path)
    item = record()
    service.capture(item, ACTOR)
    service.review(item.experience_id, reviewer_id="reviewer-1", accepted=True, actor=ACTOR)
    brief = experience_brief(
        service.retrieve(failure_class="rule_context_required", tool="rules", actor=ACTOR)
    )
    assert brief[0]["repair"].startswith("supply merchant and year")
    assert brief[0]["does_not_apply_when"]
    assert "counterexample" not in brief[0]
    assert "owner" not in brief[0]
