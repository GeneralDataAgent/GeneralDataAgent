"""The payment-document example wires an admitted specialist to a real tool.

All records, documents, and model output here are synthetic. These tests
exercise admission and exact-span retrieval, not benchmark or release evidence.
"""

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from langsmith import tracing_context

from generaldataagent.business.rules.retrieval import PassageError

ROOT = Path(__file__).resolve().parents[1]
DEMO_PATH = ROOT / "examples" / "payment_document_skill_demo.py"
SPEC = importlib.util.spec_from_file_location("payment_document_skill_demo", DEMO_PATH)
demo_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = demo_module
SPEC.loader.exec_module(demo_module)


@pytest.fixture(autouse=True)
def _disable_remote_tracing(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    with tracing_context(enabled=False):
        yield


def test_graph_runs_admitted_specialist_with_a_real_search_documents_call(tmp_path):
    demo = demo_module.build_demo(tmp_path / "admission")
    result = demo_module.invoke_demo(demo)

    assert result["synthetic"] is True
    assert [reference[0] for reference in demo.graph.references] == ["agent", "skill"]
    assert demo.graph.references[0][1:3] == ("payment-document-analyst", "v1")
    assert demo.graph.references[1][1:3] == ("payment-document-context", "v1")
    # The Skill narrowed the specialist to its one declared existing tool.
    assert demo.search_tool.name == "search_documents"
    assert demo.model.bindings[0] == ["task"]
    assert demo.model.bindings[1] == ["search_documents"]
    assert len(result["retrievals"]) == 1

    (retrieval,) = result["retrievals"]
    assert retrieval["untrusted"] is True
    (match,) = retrieval["matches"]
    assert match["citation"] == "synthetic-payment-manual.md#Domesticity:8-9"
    assert match["text"] == (
        "A transaction is domestic only when the issuer country equals the acquirer country.\n"
        "If either country is missing, do not infer a match."
    )
    lines = demo_module.SYNTHETIC_MANUAL.splitlines()
    assert lines[match["start_line"] - 1 : match["end_line"]] == match["text"].splitlines()
    assert "exact source span" in result["final"]
    assert match["citation"] in result["final"]

    # Both specialist turns saw the rendered Skill; the second also saw the
    # tool result, so the citation reached the model through the graph rather
    # than being reconstructed by the test.
    specialist_prompts = [
        prompt
        for prompt in demo.model.prompts
        if "Use the supplied public documents as evidence" in prompt[0].content
    ]
    assert len(specialist_prompts) == 2
    system_prompt = specialist_prompts[0][0].content
    assert demo_module.DEMO_QUESTION in system_prompt
    tool_messages = [
        message
        for message in specialist_prompts[1]
        if getattr(message, "name", None) == "search_documents"
    ]
    assert len(tool_messages) == 1
    assert match["citation"] in tool_messages[0].content


def test_admission_still_pins_the_exact_skill_and_agent_hashes(tmp_path):
    demo = demo_module.build_demo(tmp_path / "admission")
    changed_skill = demo.skill.model_copy(update={"verification": ("cite anything",)})
    changed_agent = replace(demo_module.PAYMENT_DOCUMENT_SPEC, system_prompt="changed")

    with pytest.raises(PermissionError, match="capability_not_admitted"):
        demo.admission.render_skill(
            changed_skill,
            demo_module.ACTOR,
            {"question": "q", "documents": "d"},
        )
    with pytest.raises(PermissionError):
        demo.admission.agents(
            (changed_agent,),
            demo_module.ACTOR,
            {changed_agent.name: "v1"},
        )
    demo.admission.agents(
        (demo.spec,),
        demo_module.ACTOR,
        {demo.spec.name: "v1"},
    )


def test_retrieval_never_falls_back_to_the_whole_document(tmp_path):
    tool = demo_module.build_search_documents(
        demo_module.SYNTHETIC_MANUAL, source="synthetic-payment-manual.md"
    )

    result = tool.invoke({"query": "empty list semantics"})

    assert result["matches"] == []
    assert result["truncated"] is False
    with pytest.raises(PassageError, match="searchable term"):
        tool.invoke({"query": "---"})
