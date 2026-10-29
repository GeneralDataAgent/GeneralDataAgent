"""Explicit Skill-bound structural checks, exercised through the rule consumer."""

import copy
import json
from dataclasses import replace
from uuid import uuid4

import pytest
from test_rule_document_extension import (
    DocumentModel,
    attach,
    extension,
    model_script,
)
from test_stage10_rule_supervisor import ACTOR, build

from generaldataagent.business.rules.document_context import MarkdownDocument
from generaldataagent.business.rules.document_verification import (
    DOCUMENT_CHECKS,
    DOCUMENT_VERIFIER_VERSION,
    DocumentEvidenceVerifier,
)
from generaldataagent.orchestration.checkpoints import open_protected_checkpointer


def checked_extension(tmp_path, documents):
    value = extension(tmp_path, documents)
    return replace(
        value,
        verifier=DocumentEvidenceVerifier(
            value.skill.name, value.skill.version, value.admission.skill_hash(value.skill)
        ),
    )


@pytest.mark.parametrize(
    "documents",
    [
        [],
        [MarkdownDocument("empty.md", "", "v1")],
        [MarkdownDocument("other.md", "unrelated convention", "v1")],
        [MarkdownDocument("scope.md", "# Heading\r\nThe scope is local.\r\n", "v2")],
        [
            MarkdownDocument("one.md", "scope is local", "v1"),
            MarkdownDocument("two.md", "scope is global", "v2"),
        ],
        [MarkdownDocument(f"scope-{i}.md", "scope quotation", "v1") for i in range(5)],
    ],
)
def test_checked_real_graph_keeps_valid_quotations_and_advisory_gaps(tmp_path, documents):
    value = checked_extension(tmp_path, documents)
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        runtime = attach(build(tmp_path, saver, governed=True), value)
        model = DocumentModel(model_script())
        runtime.model = model
        result = runtime.run(ACTOR, thread_id=str(runtime.task_id), max_runtime_seconds=10)
        assert result.status == "completed"
    messages = [message for prompt in model._prompts for message in prompt]
    observed = next(
        json.loads(message.content)
        for message in messages
        if getattr(message, "name", None) == "search_documents"
    )
    verification = observed.pop("structural_verification")
    assert verification == {
        "adapter": DOCUMENT_VERIFIER_VERSION,
        "scope": "producer_self_check",
        "passed": True,
        "check_codes": list(DOCUMENT_CHECKS),
        "skill": value.summary()["skill"],
    }
    assert value.summary()["verification_adapter"] == DOCUMENT_VERIFIER_VERSION
    observed.pop("evidence")
    assert observed == value.context.search("scope")


@pytest.mark.parametrize(
    "field",
    [
        "source",
        "text",
        "heading",
        "version",
        "content_hash",
        "passage_hash",
        "start_line",
        "end_line",
        "citation",
    ],
)
def test_changed_quotation_is_refused_before_releasing_to_next_role(tmp_path, monkeypatch, field):
    value = checked_extension(tmp_path, [MarkdownDocument("scope.md", "scope is local", "v1")])
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        runtime = attach(build(tmp_path, saver, governed=True), value)
        original = runtime.runner.search_documents

        def substitute(*args, **kwargs):
            result = copy.deepcopy(original(*args, **kwargs))
            result["matches"][0][field] = 999 if field.endswith("line") else "changed"
            return result

        monkeypatch.setattr(runtime.runner, "search_documents", substitute)
        runtime.model = DocumentModel(model_script())
        result = runtime.run(ACTOR, thread_id=str(runtime.task_id), max_runtime_seconds=10)
        assert result.status == "failed"
        assert result.delegated_roles == ("discovery",)
        assert result.artifact_uris == ()


@pytest.mark.parametrize("field", ["query", "documents", "gap", "truncated", "hit_count"])
def test_changed_result_metadata_is_refused(tmp_path, monkeypatch, field):
    value = checked_extension(tmp_path, [MarkdownDocument("scope.md", "scope is local", "v1")])
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        runtime = attach(build(tmp_path, saver, governed=True), value)
        original = runtime.runner.search_documents

        def substitute(*args, **kwargs):
            result = original(*args, **kwargs)
            return {**result, field: "changed"}

        monkeypatch.setattr(runtime.runner, "search_documents", substitute)
        groups = runtime._build_actor_tools(ACTOR, {}, [])
        groups["discovery"][0].invoke({})
        with pytest.raises(ValueError, match="document_verifier_result_mismatch"):
            groups["discovery"][1].invoke({"query": "scope"})


@pytest.mark.parametrize("change", ["task", "body", "type", "missing"])
def test_substituted_artifact_is_refused(tmp_path, monkeypatch, change):
    value = checked_extension(tmp_path, [MarkdownDocument("scope.md", "scope is local", "v1")])
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        runtime = attach(build(tmp_path, saver, governed=True), value)
        original = runtime.runner.search_documents

        def substitute(*args, **kwargs):
            result = original(*args, **kwargs)
            if change == "missing":
                return {key: item for key, item in result.items() if key != "evidence"}
            body = {key: item for key, item in result.items() if key != "evidence"}
            ref = runtime.runner.artifacts.put_json(
                task_id=uuid4() if change == "task" else runtime.task_id,
                artifact_type="other.evidence" if change == "type" else "rule.document.evidence",
                value={"changed": True} if change == "body" else body,
                created_by="substitute",
            )
            return {**result, "evidence": ref.model_dump(mode="json")}

        monkeypatch.setattr(runtime.runner, "search_documents", substitute)
        groups = runtime._build_actor_tools(ACTOR, {}, [])
        groups["discovery"][0].invoke({})
        with pytest.raises(ValueError):
            groups["discovery"][1].invoke({"query": "scope"})


def test_foreign_skill_binding_and_unsupported_adapter_are_refused(tmp_path):
    value = checked_extension(tmp_path, [])
    with pytest.raises(ValueError, match="skill_binding_mismatch"):
        replace(value, verifier=replace(value.verifier, skill_hash="sha256:" + "0" * 64))
    with pytest.raises(ValueError, match="adapter_unsupported"):
        replace(value.verifier, adapter="arbitrary-code")
    with pytest.raises(ValueError, match="adapter_unsupported"):
        replace(value, verifier=object())


def test_revocation_during_verification_blocks_release(tmp_path, monkeypatch):
    value = checked_extension(tmp_path, [MarkdownDocument("scope.md", "scope is local", "v1")])
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        runtime = attach(build(tmp_path, saver, governed=True), value)
        original = runtime.runner.artifacts.read_bytes
        reads = []

        def revoke(ref):
            content = original(ref)
            reads.append(ref)
            # The first read is the runner's persistence check; revoke during
            # the verifier's second read to exercise the final admission check.
            if len(reads) == 2:
                value.admission.registry.revoke(value.skill.name, value.skill.version)
            return content

        monkeypatch.setattr(runtime.runner.artifacts, "read_bytes", revoke)
        groups = runtime._build_actor_tools(ACTOR, {}, [])
        groups["discovery"][0].invoke({})
        with pytest.raises(PermissionError):
            groups["discovery"][1].invoke({"query": "scope"})
        assert len(reads) == 2
