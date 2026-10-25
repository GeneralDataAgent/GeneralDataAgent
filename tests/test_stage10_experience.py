import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from generaldataagent.business.experience import (
    ExperienceError,
    ExperienceRecord,
    ExperienceStore,
    experience_brief,
    experience_hints,
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


def test_capture_cannot_assign_another_owner_to_bypass_self_review(tmp_path):
    service = store(tmp_path)
    item = record(owner="someone-else")
    with pytest.raises(ExperienceError, match="experience_owner_mismatch"):
        service.capture(item, ACTOR)
    with pytest.raises(ExperienceError, match="experience_scope_mismatch"):
        service.state(item.experience_id, ACTOR)
    assert service.retrieve(failure_class=item.failure_class, tool="rules", actor=ACTOR) == ()


@pytest.mark.parametrize("operation", ["retrieve", "state", "review"])
def test_legacy_owner_mismatch_is_refused_even_with_valid_body_hash(tmp_path, operation):
    import sqlite3

    from generaldataagent.business.actions import content_hash

    service = store(tmp_path)
    item = record()
    service.capture(item, ACTOR)
    service.review(item.experience_id, reviewer_id="reviewer", accepted=True, actor=ACTOR)
    changed = item.model_copy(update={"owner": "someone-else"})
    with sqlite3.connect(service.path) as db:
        db.execute(
            "UPDATE experiences SET body=?,hash=? WHERE id=?",
            (
                changed.model_dump_json(),
                content_hash(changed.model_dump(mode="json")),
                str(item.experience_id),
            ),
        )
    with pytest.raises(ExperienceError, match="experience_integrity_failure"):
        if operation == "retrieve":
            service.retrieve(failure_class=item.failure_class, tool="rules", actor=ACTOR)
        elif operation == "state":
            service.state(item.experience_id, ACTOR)
        else:
            service.review(
                item.experience_id, reviewer_id=ACTOR.user_id, accepted=True, actor=ACTOR
            )
    with sqlite3.connect(service.path) as db:
        assert db.execute("SELECT COUNT(*) FROM experience_reviews").fetchone()[0] == 1


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


def test_a_later_independent_rejection_withdraws_an_active_record(tmp_path):
    service = store(tmp_path)
    item = record()
    service.capture(item, ACTOR)
    service.review(item.experience_id, reviewer_id="reviewer-1", accepted=True, actor=ACTOR)
    assert service.state(item.experience_id, ACTOR) == "active"
    service.review(item.experience_id, reviewer_id="reviewer-2", accepted=False, actor=ACTOR)
    # Fail closed: one rejection withdraws the record even after another
    # reviewer accepted it, and a fresh acceptance cannot revive it silently.
    assert service.state(item.experience_id, ACTOR) == "candidate"
    assert service.retrieve(failure_class="rule_context_required", tool="rules", actor=ACTOR) == ()
    service.review(item.experience_id, reviewer_id="reviewer-3", accepted=True, actor=ACTOR)
    assert service.state(item.experience_id, ACTOR) == "candidate"
    assert service.retrieve(failure_class="rule_context_required", tool="rules", actor=ACTOR) == ()


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


@pytest.mark.parametrize(
    "marker",
    ["EXPECTED_ANSWER", "Reference SQL", "Ground_Truth Label", "GOLD_LABELS"],
)
@pytest.mark.parametrize("field", ["repair", "applies_when"])
def test_answer_material_markers_are_refused_regardless_of_case(tmp_path, marker, field):
    service = store(tmp_path)
    text = f"The {marker} is available in the evaluation workbook"
    value = (text,) if field == "applies_when" else text
    item = record(**{field: value})
    with pytest.raises(ExperienceError, match="experience_contains_answer_material"):
        service.capture(item, ACTOR)
    with pytest.raises(ExperienceError, match="experience_scope_mismatch"):
        service.state(item.experience_id, ACTOR)
    assert service.retrieve(failure_class=item.failure_class, tool="rules", actor=ACTOR) == ()


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


@pytest.mark.parametrize(
    ("corruption", "operation_name"),
    [
        (corruption, operation)
        for corruption in (
            "invalid_json",
            "wrong_type",
            "missing_required_field",
            "non_object",
            "bad_hash",
        )
        for operation in ("retrieve", "state")
    ],
)
def test_retrieve_and_state_normalize_corrupt_bodies_to_integrity_failure(
    tmp_path, corruption, operation_name
):
    import json
    import sqlite3

    service = store(tmp_path)
    item = record()
    service.capture(item, ACTOR)
    service.review(item.experience_id, reviewer_id="reviewer", accepted=True, actor=ACTOR)
    bodies = {
        "invalid_json": "{",
        "wrong_type": json.dumps({"schema_version": "experience.v1", "failure_class": 7}),
        "missing_required_field": json.dumps({"schema_version": "experience.v1"}),
        "non_object": json.dumps(["not", "a", "record"]),
        "bad_hash": record(repair="a different lesson").model_dump_json(),
    }
    with sqlite3.connect(service.path) as db:
        db.execute(
            "UPDATE experiences SET body=? WHERE id=?",
            (bodies[corruption], str(item.experience_id)),
        )
    with pytest.raises(ExperienceError) as error:
        if operation_name == "retrieve":
            service.retrieve(failure_class=item.failure_class, tool="rules", actor=ACTOR)
        else:
            service.state(item.experience_id, ACTOR)
    assert error.value.code == "experience_integrity_failure"


@pytest.mark.parametrize("operation", ["retrieve", "state", "review", "audit"])
def test_record_identity_must_match_its_row_even_with_a_valid_hash(tmp_path, operation):
    import sqlite3

    from generaldataagent.business.actions import content_hash

    service = store(tmp_path)
    item = record()
    service.capture(item, ACTOR)
    service.review(item.experience_id, reviewer_id="reviewer", accepted=True, actor=ACTOR)
    replacement = record()
    with sqlite3.connect(service.path) as db:
        db.execute(
            "UPDATE experiences SET body=?,hash=? WHERE id=?",
            (
                replacement.model_dump_json(),
                content_hash(replacement.model_dump(mode="json")),
                str(item.experience_id),
            ),
        )
    if operation == "audit":
        result = service.audit()
        assert result["consistent"] is False
        assert result["findings"] == [
            {"experience_id": str(item.experience_id), "code": "unreadable_record"}
        ]
    else:
        with pytest.raises(ExperienceError, match="experience_integrity_failure"):
            if operation == "retrieve":
                service.retrieve(failure_class=item.failure_class, tool="rules", actor=ACTOR)
            elif operation == "state":
                service.state(item.experience_id, ACTOR)
            else:
                service.review(
                    item.experience_id, reviewer_id="another", accepted=True, actor=ACTOR
                )
    with sqlite3.connect(service.path) as db:
        assert db.execute("SELECT COUNT(*) FROM experience_reviews").fetchone()[0] == 1


@pytest.mark.parametrize("unrelated", ["failure_class", "tool_scope"])
def test_newer_unrelated_records_do_not_hide_matching_experience(tmp_path, unrelated):
    service = store(tmp_path)
    oldest, newest = record(), record()
    for item in (oldest, newest):
        service.capture(item, ACTOR)
        service.review(item.experience_id, reviewer_id="reviewer", accepted=True, actor=ACTOR)
    changes = (
        {"failure_class": "unknown_column"}
        if unrelated == "failure_class"
        else {"tool_scope": ("sql",)}
    )
    for _ in range(201):
        item = record(**changes)
        service.capture(item, ACTOR)
        service.review(item.experience_id, reviewer_id="reviewer", accepted=True, actor=ACTOR)
    found = service.retrieve(failure_class="rule_context_required", tool="rules", actor=ACTOR)
    assert [item.experience_id for item in found] == [newest.experience_id, oldest.experience_id]
    limited = service.retrieve(
        failure_class="rule_context_required", tool="rules", actor=ACTOR, limit=1
    )
    assert [item.experience_id for item in limited] == [newest.experience_id]


def test_integrity_checks_continue_beyond_the_first_retrieval_batch(tmp_path):
    import sqlite3

    service = store(tmp_path)
    target = record()
    service.capture(target, ACTOR)
    service.review(target.experience_id, reviewer_id="reviewer", accepted=True, actor=ACTOR)
    for _ in range(201):
        item = record(failure_class="unknown_column")
        service.capture(item, ACTOR)
        service.review(item.experience_id, reviewer_id="reviewer", accepted=True, actor=ACTOR)
    with sqlite3.connect(service.path) as db:
        db.execute(
            "UPDATE experiences SET hash=? WHERE id=?", ("tampered", str(target.experience_id))
        )
    with pytest.raises(ExperienceError, match="experience_integrity_failure"):
        service.retrieve(failure_class="rule_context_required", tool="rules", actor=ACTOR)


@pytest.mark.parametrize(
    "corruption",
    ["missing", "rejected", "self_review", "blank_reviewer", "invalid_verdict", "null_verdict"],
)
@pytest.mark.parametrize("operation", ["retrieve", "state"])
def test_active_records_require_valid_independent_review_evidence(tmp_path, corruption, operation):
    import sqlite3

    service = store(tmp_path)
    item = record()
    service.capture(item, ACTOR)
    service.review(item.experience_id, reviewer_id="reviewer", accepted=True, actor=ACTOR)
    with sqlite3.connect(service.path) as db:
        if corruption == "missing":
            db.execute("DELETE FROM experience_reviews WHERE id=?", (str(item.experience_id),))
        else:
            reviewer, verdict = {
                "rejected": ("reviewer", 0),
                "self_review": (item.owner, 1),
                "blank_reviewer": ("\t\n", 1),
                "invalid_verdict": ("reviewer", 2),
                "null_verdict": ("reviewer", None),
            }[corruption]
            db.execute(
                "UPDATE experience_reviews SET reviewer=?,accepted=? WHERE id=?",
                (reviewer, verdict, str(item.experience_id)),
            )
    with pytest.raises(ExperienceError, match="experience_integrity_failure"):
        if operation == "retrieve":
            service.retrieve(failure_class=item.failure_class, tool="rules", actor=ACTOR)
        else:
            service.state(item.experience_id, ACTOR)


@pytest.mark.parametrize("operation", ["retrieve", "state"])
def test_review_evidence_uses_the_same_read_snapshot_as_the_record(
    tmp_path, monkeypatch, operation
):
    import sqlite3

    service = store(tmp_path)
    with sqlite3.connect(service.path) as db:
        db.execute("PRAGMA journal_mode=WAL")
    item = record()
    service.capture(item, ACTOR)
    service.review(item.experience_id, reviewer_id="reviewer", accepted=True, actor=ACTOR)
    decode = service._decode_record

    def withdraw_after_record_read(body, digest, experience_id):
        result = decode(body, digest, experience_id)
        with sqlite3.connect(service.path) as db:
            db.execute("UPDATE experience_reviews SET accepted=0")
            db.execute("UPDATE experiences SET state='candidate'")
        return result

    monkeypatch.setattr(service, "_decode_record", withdraw_after_record_read)
    if operation == "retrieve":
        found = service.retrieve(failure_class=item.failure_class, tool="rules", actor=ACTOR)
        assert [row.experience_id for row in found] == [item.experience_id]
    else:
        assert service.state(item.experience_id, ACTOR) == "active"
    monkeypatch.setattr(service, "_decode_record", decode)
    assert service.state(item.experience_id, ACTOR) == "candidate"
    assert service.retrieve(failure_class=item.failure_class, tool="rules", actor=ACTOR) == ()


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


def test_hints_are_bounded_by_a_character_budget(tmp_path):
    service = store(tmp_path)
    long_record = record(
        repair="supply merchant and year " + "x" * 1800,
        applies_when=("a contextual rule operation was attempted without a merchant",),
    )
    service.capture(long_record, ACTOR)
    service.review(long_record.experience_id, reviewer_id="reviewer-1", accepted=True, actor=ACTOR)
    hints = experience_hints(service, ACTOR)
    briefs = hints("rule_context_required", "rules")
    assert hints.last_budget["budget"] == 2000
    assert hints.last_budget["truncated"] == 1
    # A brief that cannot fit is dropped whole rather than cut mid-sentence.
    assert briefs == ()


def test_hints_fit_as_many_whole_briefs_as_the_budget_allows(tmp_path):
    service = store(tmp_path)
    small = record()
    service.capture(small, ACTOR)
    service.review(small.experience_id, reviewer_id="reviewer-1", accepted=True, actor=ACTOR)
    hints = experience_hints(service, ACTOR)
    briefs = hints("rule_context_required", "rules")
    assert len(briefs) == 1
    assert hints.last_budget["truncated"] == 0
    assert hints.last_budget["used"] <= hints.last_budget["budget"]


@pytest.mark.parametrize("repair", ["核对来源后再重试" * 30, "check source " * 30])
def test_hint_budget_matches_the_serialized_prompt_list(tmp_path, repair):
    service = store(tmp_path)
    item = record(repair=repair)
    service.capture(item, ACTOR)
    service.review(item.experience_id, reviewer_id="reviewer-1", accepted=True, actor=ACTOR)
    expected = list(experience_brief((item,)))
    rendered_size = len(json.dumps(expected))
    hints = experience_hints(service, ACTOR, char_budget=rendered_size - 1)
    assert hints(item.failure_class, "rules") == ()
    assert hints.last_budget["used"] == 0
    assert hints.last_budget["truncated"] == 1

    hints = experience_hints(service, ACTOR, char_budget=rendered_size)
    briefs = hints(item.failure_class, "rules")
    assert list(briefs) == expected
    assert hints.last_budget["used"] == len(json.dumps(list(briefs))) == rendered_size


def test_hint_budget_includes_separators_between_records(tmp_path):
    service = store(tmp_path)
    items = (record(), record())
    for item in items:
        service.capture(item, ACTOR)
        service.review(item.experience_id, reviewer_id="reviewer-1", accepted=True, actor=ACTOR)
    rendered_size = len(json.dumps(list(experience_brief(items))))
    hints = experience_hints(service, ACTOR, char_budget=rendered_size - 1)
    briefs = hints(items[0].failure_class, "rules")
    assert len(briefs) == 1
    assert hints.last_budget["used"] == len(json.dumps(list(briefs)))
    assert hints.last_budget["truncated"] == 1


def test_failed_hint_lookup_clears_previous_budget_counts(tmp_path, monkeypatch):
    service = store(tmp_path)
    item = record()
    service.capture(item, ACTOR)
    service.review(item.experience_id, reviewer_id="reviewer-1", accepted=True, actor=ACTOR)
    hints = experience_hints(service, ACTOR, char_budget=1)
    assert hints(item.failure_class, "rules") == ()
    assert hints.last_budget["truncated"] == 1

    def unavailable(**kwargs):
        raise RuntimeError("store offline")

    monkeypatch.setattr(service, "retrieve", unavailable)
    with pytest.raises(RuntimeError, match="store offline"):
        hints(item.failure_class, "rules")
    assert hints.last_budget == {"budget": 1, "used": 0, "truncated": 0, "offered": 0}


def test_hint_budget_rejects_a_nonpositive_limit(tmp_path):
    service = store(tmp_path)
    for value in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match="experience hint budget"):
            experience_hints(service, ACTOR, char_budget=value)
