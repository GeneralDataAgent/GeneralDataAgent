from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver

from generaldataagent.orchestration import PhaseOneServices, build_phase_one_graph


def services(validation_status: str = "pass") -> PhaseOneServices:
    return PhaseOneServices(
        discover=lambda state: {"semantic_context": {"metric": "conversion_rate", "version": "v1"}},
        plan=lambda state: {"task_plan": {"steps": ["analyze", "validate", "deliver"]}},
        analyze=lambda state: {
            "analysis_result": {"claims": [{"text": "Conversion is 42%", "evidence": ["a1"]}]}
        },
        validate=lambda state: {"validation_result": {"status": validation_status}},
        deliver=lambda state: {
            "status": "completed",
            "delivery_package": {"summary": state["analysis_result"]["claims"][0]["text"]},
        },
    )


def initial_state() -> dict[str, str]:
    return {
        "task_id": str(uuid4()),
        "user_id": "user-1",
        "tenant_id": "tenant-1",
        "project_id": "project-1",
        "user_query": "What is conversion?",
        "status": "created",
    }


def test_workflow_delivers_only_after_validation_passes() -> None:
    graph = build_phase_one_graph(services(), checkpointer=InMemorySaver())
    result = graph.invoke(initial_state(), {"configurable": {"thread_id": "thread-pass"}})
    assert result["status"] == "completed"
    assert "delivery_package" in result


def test_workflow_blocks_failed_validation() -> None:
    graph = build_phase_one_graph(services("fail"), checkpointer=InMemorySaver())
    result = graph.invoke(initial_state(), {"configurable": {"thread_id": "thread-fail"}})
    assert result["status"] == "failed"
    assert "delivery_package" not in result
