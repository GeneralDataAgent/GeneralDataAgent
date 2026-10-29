"""Explicit dependency definitions reach the governed rule document consumer."""

import json
from dataclasses import replace

import pytest
from admission_helpers import enroll
from test_rule_document_configuration import configuration
from test_rule_document_extension import DocumentModel, attach, extension, model_script
from test_stage10_rule_supervisor import ACTOR, build

from generaldataagent.business.extensions import SkillDefinition
from generaldataagent.business.rules.document_context import MarkdownDocument
from generaldataagent.business.rules.document_verification import DocumentEvidenceVerifier
from generaldataagent.business.rules.governed import GovernedRuleRunner
from generaldataagent.orchestration.checkpoints import open_protected_checkpointer


def with_bundle(value, *, actor=ACTOR):
    leaf = SkillDefinition.model_validate(
        value.skill.model_dump(mode="json")
        | {
            "name": "rule-citation-policy",
            "prompt_template": "Citation prerequisite: use exact spans from $document_manifest.",
            "allowed_tools": ["search_documents"],
            "budget": {"max_tool_calls": 2, "max_tokens": 1000, "max_seconds": 10},
        }
    )
    root = SkillDefinition.model_validate(
        value.skill.model_dump(mode="json")
        | {"version": "v2", "dependencies": [f"{leaf.name}@{leaf.version}"]}
    )
    for item in (root, leaf):
        enroll(
            value.admission.registry,
            actor,
            "skill",
            item.name,
            value.admission.skill_hash(item),
            version=item.version,
        )
    return replace(value, skill=root, skill_dependencies={f"{leaf.name}@{leaf.version}": leaf})


def configured_bundle(tmp_path):
    module, path, config, value = configuration(tmp_path)
    value = with_bundle(value, actor=module.ACTOR)
    leaf = next(iter(value.skill_dependencies.values()))
    (tmp_path / "skill.json").write_text(value.skill.model_dump_json())
    (tmp_path / "citation.json").write_text(leaf.model_dump_json())
    config["skill_hash"] = value.admission.skill_hash(value.skill)
    config["skill_dependencies"] = [
        {"path": "citation.json", "skill_hash": value.admission.skill_hash(leaf)}
    ]
    path.write_text(json.dumps(config))
    return module, path, config, value


def test_configured_bundle_pins_every_definition_and_keeps_an_immutable_snapshot(tmp_path):
    module, path, config, value = configured_bundle(tmp_path)
    loaded = module.load_document_extension(path)
    assert loaded.summary() == value.summary()
    assert (
        loaded.summary()["skill_dependencies"][0]["content_hash"]
        == config["skill_dependencies"][0]["skill_hash"]
    )
    supplied = dict(value.skill_dependencies)
    snapshot = replace(value, skill_dependencies=supplied)
    supplied.clear()
    assert snapshot.summary() == value.summary()
    with pytest.raises(TypeError):
        snapshot.skill_dependencies["other@v1"] = value.skill
    for definitions in (None, [], {"wrong@v1": value.skill}, {"bad@v1": object()}):
        with pytest.raises(ValueError, match="skill_dependency_definition"):
            replace(value, skill_dependencies=definitions)


def test_loaded_bundle_configuration_runs_the_real_consumer(tmp_path):
    module, path, config, _ = configured_bundle(tmp_path)
    config["verification_adapter"] = "document-evidence-v1"
    path.write_text(json.dumps(config))
    value = module.load_document_extension(path)
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        base = build(tmp_path, saver, governed=True)
        base.runner = GovernedRuleRunner(
            runtime=base.workflow,
            actor=module.ACTOR,
            artifacts=base.runner.artifacts,
            audit=base.runner.audit,
        )
        runtime = attach(base, value)
        runtime.model = DocumentModel(model_script())
        result = runtime.run(module.ACTOR, thread_id=str(runtime.task_id), max_runtime_seconds=10)
        assert result.status == "completed"
        assert result.delegated_roles == ("discovery", "data-analyst", "verifier", "reporter")
        assert value.summary()["skill_dependencies"][0]["name"] == "rule-citation-policy"


@pytest.mark.parametrize(
    "change,code",
    [
        ("missing", "definition_missing"),
        ("pin", "pinned hash"),
        ("duplicate", "duplicate skill dependency"),
        ("outside", "inside the repository"),
        ("invalid", "explicit entries"),
        ("null", "explicit entries"),
        ("count", "explicit entries"),
        ("unknown_entry", "pinned skill dependency"),
        ("oversized", "skill input is too large"),
        ("changed", "capability_not_admitted"),
        ("revoked", "capability_not_admitted"),
        ("domain", "domains_exceed_parent"),
        ("inputs", "missing_declared_inputs"),
    ],
)
def test_dependency_configuration_is_refused_before_provider_construction(tmp_path, change, code):
    module, path, config, value = configured_bundle(tmp_path)
    entries = config["skill_dependencies"]
    leaf = next(iter(value.skill_dependencies.values()))
    if change == "missing":
        del config["skill_dependencies"]
    elif change == "pin":
        entries[0]["skill_hash"] = "sha256:" + "0" * 64
    elif change == "duplicate":
        entries.append(dict(entries[0]))
    elif change == "outside":
        entries[0]["path"] = "../citation.json"
    elif change == "invalid":
        config["skill_dependencies"] = {}
    elif change == "null":
        config["skill_dependencies"] = None
    elif change == "count":
        config["skill_dependencies"] *= 65
    elif change == "unknown_entry":
        entries[0]["extra"] = True
    elif change == "oversized":
        (tmp_path / "citation.json").write_bytes(b" " * 128_001)
    elif change == "revoked":
        value.admission.registry.revoke(leaf.name, leaf.version)
    else:
        changes = {
            "changed": {"prompt_template": "Changed procedure"},
            "domain": {"required_data_domains": ["foreign"]},
            "inputs": {"inputs": ["unavailable_input"], "prompt_template": "$unavailable_input"},
        }
        leaf = SkillDefinition.model_validate(leaf.model_dump(mode="json") | changes[change])
        if change == "inputs":
            leaf = leaf.model_copy(update={"version": "v2"})
            root = value.skill.model_copy(
                update={"version": "v3", "dependencies": (f"{leaf.name}@v2",)}
            )
            for item in (root, leaf):
                enroll(
                    value.admission.registry,
                    module.ACTOR,
                    "skill",
                    item.name,
                    value.admission.skill_hash(item),
                    version=item.version,
                )
            (tmp_path / "skill.json").write_text(root.model_dump_json())
            config["skill_hash"] = value.admission.skill_hash(root)
        (tmp_path / "citation.json").write_text(leaf.model_dump_json())
        entries[0]["skill_hash"] = value.admission.skill_hash(leaf)
    path.write_text(json.dumps(config))
    with pytest.raises((ValueError, PermissionError), match=code):
        module.load_document_extension(path)


@pytest.mark.parametrize("documents", [[], [MarkdownDocument("scope.md", "scope is local", "v1")]])
def test_bundle_reaches_real_graph_without_changing_rules_or_root_verifier(tmp_path, documents):
    value = with_bundle(extension(tmp_path, documents))
    value = replace(
        value,
        verifier=DocumentEvidenceVerifier(
            value.skill.name, value.skill.version, value.admission.skill_hash(value.skill)
        ),
    )
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        runtime = attach(build(tmp_path, saver, governed=True), value)
        model = DocumentModel(model_script())
        runtime.model = model
        original_request = runtime.request.model_dump()
        original_sources = dict(runtime.workflow.source_hashes())
        result = runtime.run(ACTOR, thread_id=str(runtime.task_id), max_runtime_seconds=10)
        assert result.status == "completed"
        state = runtime.workflow.state_of(str(runtime.task_id), runtime.request, ACTOR)
        assert state["computation"]["average_fee"] == "0.092"
        assert runtime.request.model_dump() == original_request
        assert runtime.workflow.source_hashes() == original_sources
    prompts = [str(prompt[0].content) for prompt in model._prompts]
    assert any("Citation prerequisite:" in p and "Resolve " in p for p in prompts)
    messages = [message for prompt in model._prompts for message in prompt]
    retrieval = next(
        json.loads(message.content)
        for message in messages
        if getattr(message, "name", None) == "search_documents"
    )
    assert retrieval["structural_verification"]["skill"] == value.summary()["skill"]
    assert retrieval["structural_verification"]["scope"] == "producer_self_check"
    assert retrieval["gap"] == (None if documents else "no_documents")
    assert retrieval["matches"] == value.context.search("scope")["matches"]


@pytest.mark.parametrize("phase", ["before_search", "during_search", "during_verification"])
def test_dependency_revocation_blocks_quotation_release(tmp_path, monkeypatch, phase):
    value = with_bundle(extension(tmp_path, [MarkdownDocument("scope.md", "scope is local", "v1")]))
    value = replace(
        value,
        verifier=DocumentEvidenceVerifier(
            value.skill.name, value.skill.version, value.admission.skill_hash(value.skill)
        ),
    )
    leaf = next(iter(value.skill_dependencies.values()))

    def revoke():
        value.admission.registry.revoke(leaf.name, leaf.version)

    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        runtime = attach(build(tmp_path, saver, governed=True), value)
        groups = runtime._build_actor_tools(ACTOR, {}, [])
        groups["discovery"][0].invoke({})
        reads = []
        if phase == "before_search":
            revoke()
            monkeypatch.setattr(value.context, "search", lambda query: reads.append(query))
        elif phase == "during_search":
            original = runtime.runner.audit.emit

            def emit(event):
                original(event)
                revoke()

            monkeypatch.setattr(runtime.runner.audit, "emit", emit)
        else:
            original = runtime.runner.artifacts.read_bytes

            def read(ref):
                content = original(ref)
                reads.append(ref)
                if len(reads) == 2:
                    revoke()
                return content

            monkeypatch.setattr(runtime.runner.artifacts, "read_bytes", read)
        with pytest.raises(PermissionError, match="capability_not_admitted"):
            groups["discovery"][1].invoke({"query": "scope"})
        if phase == "before_search":
            assert reads == []
        elif phase == "during_verification":
            assert len(reads) == 2
