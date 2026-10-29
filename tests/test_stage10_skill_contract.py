from datetime import UTC, datetime

import pytest
from admission_helpers import enroll

from generaldataagent.business.extensions import ExtensionAdmission, SkillDefinition
from generaldataagent.business.governance import CapabilityRegistry
from generaldataagent.contracts import ActorContext, Budget

ACTOR = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("commerce",))
EXAMPLES = (
    dict(kind="positive", request="monthly revenue", expected="the certified metric"),
    dict(kind="negative", request="guess a metric", expected="explicit ambiguity"),
)


def definition(**changes):
    values = dict(
        name="metric-lookup",
        version="v1",
        prompt_template="Use $domain definitions for $question.",
        allowed_tools=("catalog.search",),
        applicability=("certified metric lookup",),
        inputs=("domain", "question"),
        outputs=("metric definition",),
        budget=dict(max_tool_calls=3, max_tokens=1000, max_seconds=60),
        verification=("cite the catalog entry",),
        failure_handling=("report ambiguity instead of guessing",),
        examples=EXAMPLES,
    )
    return SkillDefinition(**(values | changes))


def admission(tmp_path, skill=None):
    registry = CapabilityRegistry(tmp_path / "registry", clock=lambda: datetime.now(UTC))
    service = ExtensionAdmission(registry)
    skill = skill or definition()
    enroll(registry, ACTOR, "skill", skill.name, service.skill_hash(skill))
    return service, skill


def test_definition_requires_the_full_contract():
    with pytest.raises(ValueError):
        SkillDefinition(name="x", version="v1", prompt_template="t", allowed_tools=("a",))


def test_definition_rejects_a_missing_negative_example():
    # Two positives satisfy the length bound but still fail the kind check.
    with pytest.raises(ValueError, match="positive and one negative"):
        definition(examples=(EXAMPLES[0], EXAMPLES[0]))
    with pytest.raises(ValueError, match="positive and one negative"):
        definition(examples=(EXAMPLES[1], EXAMPLES[1]))


def test_definition_rejects_blank_duplicate_and_malformed_declarations():
    with pytest.raises(ValueError):
        definition(allowed_tools=("a", "a"))
    with pytest.raises(ValueError):
        definition(applicability=("  ",))
    with pytest.raises(ValueError, match="name@version"):
        definition(dependencies=("other",))
    with pytest.raises(ValueError, match="cannot depend on itself"):
        definition(dependencies=("metric-lookup@v2",))
    with pytest.raises(ValueError):
        definition(verification=())


@pytest.mark.parametrize("field", ["verification", "failure_handling"])
@pytest.mark.parametrize("members", [("",), (" \t\n",), ("valid declaration", " ")])
@pytest.mark.parametrize("from_json", [False, True])
def test_blank_verification_and_failure_declarations_are_refused(field, members, from_json):
    if from_json:
        import json

        stored = definition().model_dump(mode="json")
        stored[field] = members
        with pytest.raises(ValueError, match="skill declarations must not be blank"):
            SkillDefinition.model_validate_json(json.dumps(stored))
    else:
        with pytest.raises(ValueError, match="skill declarations must not be blank"):
            definition(**{field: members})


def test_valid_verification_and_failure_text_keeps_its_exact_bytes_and_hash():
    from generaldataagent.business.actions import content_hash

    skill = definition(
        verification=(" cite the catalog entry ",),
        failure_handling=(" report ambiguity ", " report ambiguity "),
    )
    stored = skill.model_dump(mode="json")
    assert stored["verification"] == [" cite the catalog entry "]
    assert stored["failure_handling"] == [" report ambiguity ", " report ambiguity "]
    assert ExtensionAdmission.skill_hash(skill) == content_hash({"kind": "skill", "spec": stored})
    assert SkillDefinition.model_validate_json(skill.model_dump_json()) == skill


def test_hash_covers_every_declared_field():
    base = ExtensionAdmission.skill_hash(definition())
    for change in (
        dict(verification=("cite two entries",)),
        dict(budget=dict(max_tool_calls=4, max_tokens=1000, max_seconds=60)),
        dict(dependencies=("other-skill@v1",)),
        dict(applicability=("certified metric lookup", "metric lineage")),
    ):
        assert ExtensionAdmission.skill_hash(definition(**change)) != base


def test_render_requires_every_declared_input(tmp_path):
    service, skill = admission(tmp_path)
    with pytest.raises(ValueError, match="skill_context_missing_declared_inputs"):
        service.render_skill(skill, ACTOR, {"domain": "commerce"})
    text, reference = service.render_skill(
        skill, ACTOR, {"domain": "commerce", "question": "revenue"}
    )
    assert "commerce definitions for revenue" in text
    assert reference == ("skill", "metric-lookup", "v1", service.skill_hash(skill))


def test_render_cannot_widen_data_domains(tmp_path):
    # Each admission pins one immutable (name, version) record, so the
    # restricted and permitted variants use distinct versions.
    service = ExtensionAdmission(
        CapabilityRegistry(tmp_path / "registry", clock=lambda: datetime.now(UTC))
    )
    restricted = definition(version="v1", required_data_domains=("commerce", "payments"))
    enroll(service.registry, ACTOR, "skill", restricted.name, service.skill_hash(restricted))
    with pytest.raises(PermissionError, match="skill_data_domain_not_granted"):
        service.render_skill(restricted, ACTOR, {"domain": "commerce", "question": "revenue"})
    # A domain the actor holds is fine.
    narrow = definition(version="v2", required_data_domains=("commerce",))
    enroll(
        service.registry,
        ACTOR,
        "skill",
        narrow.name,
        service.skill_hash(narrow),
        version="v2",
    )
    service.render_skill(narrow, ACTOR, {"domain": "commerce", "question": "revenue"})


def test_render_cannot_exceed_the_task_budget(tmp_path):
    service, skill = admission(tmp_path)
    context = {"domain": "commerce", "question": "revenue"}
    service.render_skill(
        skill,
        ACTOR,
        context,
        task_budget=Budget(max_tool_calls=3, max_tokens=1000, max_runtime_seconds=60),
    )
    with pytest.raises(PermissionError, match="skill_budget_exceeds_task_budget"):
        service.render_skill(
            skill,
            ACTOR,
            context,
            task_budget=Budget(max_tool_calls=2, max_tokens=1000, max_runtime_seconds=60),
        )
    with pytest.raises(PermissionError, match="skill_budget_exceeds_task_budget"):
        service.render_skill(
            skill,
            ACTOR,
            context,
            task_budget=Budget(max_tool_calls=3, max_tokens=999, max_runtime_seconds=60),
        )


def test_contract_changes_invalidate_admission(tmp_path):
    service, skill = admission(tmp_path)
    context = {"domain": "commerce", "question": "revenue"}
    service.render_skill(skill, ACTOR, context)
    changed = skill.model_copy(update={"verification": ("skip the check",)})
    with pytest.raises(PermissionError, match="capability_not_admitted"):
        service.render_skill(changed, ACTOR, context)
