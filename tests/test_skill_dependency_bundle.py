"""Dependency assembly is explicit, bounded, monotone and pinned through use."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from admission_helpers import enroll
from test_stage10_skill_contract import ACTOR, definition

from generaldataagent.agents.factory import build_supervisor
from generaldataagent.agents.specs import SPECIALIST_SPECS
from generaldataagent.business.extensions import ExtensionAdmission
from generaldataagent.business.governance import CapabilityRegistry
from generaldataagent.contracts import Budget

CONTEXT = {"domain": "commerce", "question": "revenue"}


def skill(name, **changes):
    return definition(name=name, required_data_domains=("commerce",), **changes)


def service(tmp_path, skills):
    admission = ExtensionAdmission(
        CapabilityRegistry(tmp_path / "registry", clock=lambda: datetime.now(UTC))
    )
    for item in skills:
        enroll(
            admission.registry,
            ACTOR,
            "skill",
            item.name,
            admission.skill_hash(item),
            version=item.version,
        )
    return admission


def keyed(skills):
    return {f"{item.name}@{item.version}": item for item in skills}


def test_single_skill_text_reference_and_legacy_hash_are_unchanged(tmp_path):
    root = skill("root")
    admission = service(tmp_path, [root])
    old_text, old_ref = admission.render_skill(root, ACTOR, CONTEXT)
    text, refs = admission.render_skill_bundle(root, ACTOR, CONTEXT)
    assert text == old_text and refs == (old_ref,)


def test_diamond_is_rendered_once_in_declared_topological_order(tmp_path):
    leaf = skill("shared", prompt_template="Shared $domain citation policy.")
    left = skill("left", dependencies=("shared@v1",), prompt_template="Left $question procedure.")
    right = skill(
        "right", dependencies=("shared@v1",), prompt_template="Right $question procedure."
    )
    root = skill(
        "root", dependencies=("left@v1", "right@v1"), prompt_template="Root $question output."
    )
    admission = service(tmp_path, [root, left, right, leaf])
    text, refs = admission.render_skill_bundle(
        root, ACTOR, CONTEXT, dependencies=keyed([left, right, leaf])
    )
    assert [ref[1] for ref in refs] == ["shared", "left", "right", "root"]
    assert text.count("Shared commerce citation policy.") == 1
    assert (
        text.index("Left revenue procedure")
        < text.index("Right revenue procedure")
        < text.index("Root revenue output")
    )
    assert all(
        ref[3] == admission.skill_hash(item)
        for ref, item in zip(refs, (leaf, left, right, root), strict=True)
    )


def test_dependency_requires_a_definition_and_legacy_render_refuses_to_ignore_it(tmp_path):
    root = skill("root", dependencies=("leaf@v1",))
    admission = service(tmp_path, [root])
    with pytest.raises(ValueError, match="skill_dependencies_require_bundle"):
        admission.render_skill(root, ACTOR, CONTEXT)
    with pytest.raises(ValueError, match="skill_dependency_definition_missing"):
        admission.render_skill_bundle(root, ACTOR, CONTEXT)


@pytest.mark.parametrize(
    "change,code",
    [
        ({"allowed_tools": ("deploy",)}, "tools"),
        ({"required_data_domains": ("finance",)}, "domains"),
        ({"budget": {"max_tool_calls": 4, "max_tokens": 1000, "max_seconds": 60}}, "budget"),
        ({"budget": {"max_tool_calls": 3, "max_tokens": 1001, "max_seconds": 60}}, "budget"),
        ({"budget": {"max_tool_calls": 3, "max_tokens": 1000, "max_seconds": 61}}, "budget"),
    ],
)
def test_each_dependency_is_bounded_by_its_parent(tmp_path, change, code):
    root = skill("root", dependencies=("leaf@v1",))
    values = (
        definition(name="leaf").model_dump(mode="json")
        | {"required_data_domains": ["commerce"]}
        | change
    )
    from generaldataagent.business.extensions import SkillDefinition

    leaf = SkillDefinition.model_validate(values)
    admission = service(tmp_path, [root, leaf])
    with pytest.raises(PermissionError, match=f"skill_dependency_{code}_exceed"):
        admission.render_skill_bundle(root, ACTOR, CONTEXT, dependencies=keyed([leaf]))


def test_child_cannot_hide_widening_under_a_broader_root(tmp_path):
    root = skill("root", allowed_tools=("catalog.search", "sql"), dependencies=("middle@v1",))
    middle = skill("middle", dependencies=("leaf@v1",))
    leaf = skill("leaf", allowed_tools=("sql",))
    admission = service(tmp_path, [root, middle, leaf])
    with pytest.raises(PermissionError, match="skill_dependency_tools_exceed_parent"):
        admission.render_skill_bundle(root, ACTOR, CONTEXT, dependencies=keyed([middle, leaf]))


@pytest.mark.parametrize("condition", ["unadmitted", "changed", "revoked"])
def test_dependency_content_and_admission_must_match(tmp_path, condition):
    root, leaf = skill("root", dependencies=("leaf@v1",)), skill("leaf")
    admission = service(tmp_path, [root] if condition == "unadmitted" else [root, leaf])
    if condition == "changed":
        leaf = leaf.model_copy(update={"prompt_template": "Changed reviewed procedure"})
    if condition == "revoked":
        admission.registry.revoke("leaf", "v1")
    with pytest.raises(PermissionError, match="capability_not_admitted"):
        admission.render_skill_bundle(root, ACTOR, CONTEXT, dependencies=keyed([leaf]))


def test_cycles_and_conflicting_versions_are_rejected(tmp_path):
    root, leaf = skill("root", dependencies=("leaf@v1",)), skill("leaf", dependencies=("root@v1",))
    admission = service(tmp_path, [root, leaf])
    with pytest.raises(ValueError, match="skill_dependency_cycle"):
        admission.render_skill_bundle(root, ACTOR, CONTEXT, dependencies=keyed([leaf]))
    root_v2 = skill("root", version="v2")
    leaf_v2 = skill("leaf", version="v2", dependencies=("root@v2",))
    root_new = skill("new-root", dependencies=("root@v1", "leaf@v2"))
    # The first path still contains the earlier cycle, so use a separate acyclic v1 root.
    root_v1 = skill("root")
    admission = service(tmp_path / "other", [root_new, root_v1, root_v2, leaf_v2])
    with pytest.raises(ValueError, match="skill_dependency_version_conflict"):
        admission.render_skill_bundle(
            root_new, ACTOR, CONTEXT, dependencies=keyed([root_v1, root_v2, leaf_v2])
        )


def test_definition_keys_and_conflicting_root_copies_are_refused(tmp_path):
    root = skill("root")
    admission = service(tmp_path, [root])
    for definitions in ({"wrong@v1": root}, {"root@v1": "not a definition"}):
        with pytest.raises(ValueError, match="skill_dependency_definition_mismatch"):
            admission.render_skill_bundle(root, ACTOR, CONTEXT, dependencies=definitions)
    with pytest.raises(ValueError, match="skill_dependency_root_definition_mismatch"):
        admission.render_skill_bundle(
            root,
            ACTOR,
            CONTEXT,
            dependencies={"root@v1": root.model_copy(update={"prompt_template": "different"})},
        )


def test_closure_depth_count_input_and_text_are_bounded(tmp_path, monkeypatch):
    root = skill("root")
    admission = service(tmp_path, [root])
    monkeypatch.setattr(admission, "require", lambda *args: None)
    with pytest.raises(ValueError, match="skill_dependency_definitions_invalid"):
        admission.render_skill_bundle(root, ACTOR, CONTEXT, dependencies=[])
    cache = keyed([skill(f"node-{n}") for n in range(65)])
    with pytest.raises(ValueError, match="skill_dependency_definitions_limit"):
        admission.render_skill_bundle(root, ACTOR, CONTEXT, dependencies=cache)
    chain = [
        skill(f"node-{n}", dependencies=(f"node-{n + 1}@v1",) if n < 8 else ()) for n in range(9)
    ]
    with pytest.raises(ValueError, match="skill_dependency_depth_limit"):
        admission.render_skill_bundle(chain[0], ACTOR, CONTEXT, dependencies=keyed(chain[1:]))
    leaves = [skill(f"leaf-{n}") for n in range(32)]
    left = skill("left", dependencies=tuple(f"leaf-{n}@v1" for n in range(16)))
    right = skill("right", dependencies=tuple(f"leaf-{n}@v1" for n in range(16, 32)))
    big = skill("big", dependencies=("left@v1", "right@v1"))
    with pytest.raises(ValueError, match="skill_dependency_count_limit"):
        admission.render_skill_bundle(
            big, ACTOR, CONTEXT, dependencies=keyed([left, right, *leaves])
        )
    leaf = skill("leaf", prompt_template="$question")
    root = skill("root", dependencies=("leaf@v1",), prompt_template="$question")
    with pytest.raises(ValueError, match="rendered_skill_bundle_too_large"):
        admission.render_skill_bundle(
            root, ACTOR, CONTEXT | {"question": "x" * 8100}, dependencies=keyed([leaf])
        )


def test_context_is_one_literal_snapshot_and_missing_child_inputs_fail(tmp_path, monkeypatch):
    leaf = skill("leaf", prompt_template="Leaf $question")
    root = skill("root", dependencies=("leaf@v1",), prompt_template="Root $question")
    admission = service(tmp_path, [root, leaf])
    context = dict(CONTEXT)
    require = admission.require

    def mutate(*args):
        context["question"] = "changed after snapshot"
        return require(*args)

    monkeypatch.setattr(admission, "require", mutate)
    text, _ = admission.render_skill_bundle(root, ACTOR, context, dependencies=keyed([leaf]))
    assert "Leaf revenue" in text and "Root revenue" in text and "changed after" not in text
    with pytest.raises(ValueError, match="skill_context_missing_declared_inputs"):
        admission.render_skill_bundle(
            root, ACTOR, {"domain": "commerce"}, dependencies=keyed([leaf])
        )
    with pytest.raises(PermissionError, match="skill_budget_exceeds_task_budget"):
        admission.render_skill_bundle(
            root,
            ACTOR,
            CONTEXT,
            dependencies=keyed([leaf]),
            task_budget=Budget(max_tool_calls=2, max_tokens=1000, max_runtime_seconds=60),
        )


def test_revocation_while_rendering_refuses_the_bundle(tmp_path, monkeypatch):
    root, leaf = skill("root", dependencies=("leaf@v1",)), skill("leaf")
    admission = service(tmp_path, [root, leaf])
    require = admission.require

    def revoke(*args):
        result = require(*args)
        if args[1] == "leaf":
            admission.registry.revoke("root", "v1")
        return result

    monkeypatch.setattr(admission, "require", revoke)
    with pytest.raises(PermissionError, match="capability_not_admitted"):
        admission.render_skill_bundle(root, ACTOR, CONTEXT, dependencies=keyed([leaf]))


def test_dependency_declared_inputs_are_checked_independently_of_the_root(tmp_path):
    root = skill("root", dependencies=("leaf@v1",))
    leaf = skill("leaf", inputs=("citation",), prompt_template="Use $citation")
    admission = service(tmp_path, [root, leaf])
    with pytest.raises(ValueError, match="skill_context_missing_declared_inputs"):
        admission.render_skill_bundle(root, ACTOR, CONTEXT, dependencies=keyed([leaf]))


def test_factory_retains_dependency_references_and_rechecks_them_on_use(tmp_path, monkeypatch):
    root, leaf = skill("root", dependencies=("leaf@v1",)), skill("leaf")
    admission = service(tmp_path, [root, leaf])
    captured = {}

    class Graph:
        calls = 0

        def invoke(self, *args, **kwargs):
            self.calls += 1
            return {"ok": True}

    inner = Graph()

    def build(**kwargs):
        captured.update(kwargs)
        return inner

    monkeypatch.setattr("generaldataagent.agents.factory.create_deep_agent", build)
    monkeypatch.setattr("generaldataagent.agents.factory._register_governed_profiles", lambda: None)
    spec = SPECIALIST_SPECS[0]
    graph = build_supervisor(
        model=None,
        tool_groups={
            spec.tool_group: [SimpleNamespace(name="catalog.search"), SimpleNamespace(name="extra")]
        },
        specialist_specs=(spec,),
        admission=admission,
        actor=ACTOR,
        specialist_skills={spec.name: root},
        skill_context=CONTEXT,
        skill_dependencies=keyed([leaf]),
    )
    assert [reference[1] for reference in graph.references] == ["leaf", "root"]
    assert [tool.name for tool in captured["subagents"][0]["tools"]] == ["catalog.search"]
    assert graph.invoke({}) == {"ok": True}
    admission.registry.revoke("leaf", "v1")
    with pytest.raises(PermissionError, match="capability_not_admitted"):
        graph.invoke({})
    assert inner.calls == 1
