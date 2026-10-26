import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from generaldataagent.evaluation.rule_diagnostics import (
    RuleCallDiagnostics,
    stop_diagnostic,
    summarize_rule_run,
)


def response(
    diagnostics,
    *,
    usage=None,
    calls=(),
    text="",
    invalid=(),
    agent=None,
    finish_reason=None,
    run_id="run-1",
    tool_choice=None,
):
    diagnostics.on_chat_model_start(
        {},
        [],
        run_id=run_id,
        metadata={"lc_agent_name": agent} if agent else None,
        invocation_params={"tool_choice": tool_choice} if tool_choice else {},
    )
    diagnostics.on_llm_end(
        LLMResult(
            generations=[
                [
                    ChatGeneration(
                        message=AIMessage(
                            content=text,
                            tool_calls=list(calls),
                            invalid_tool_calls=list(invalid),
                            usage_metadata=usage,
                            response_metadata=(
                                {"finish_reason": finish_reason} if finish_reason else {}
                            ),
                        )
                    )
                ]
            ]
        ),
        run_id=run_id,
    )


TOKENS = {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15}


def test_missing_usage_is_unknown_instead_of_complete_zero():
    diagnostics = RuleCallDiagnostics()
    assert diagnostics.usage()["telemetry_complete"] is False
    response(diagnostics)
    assert diagnostics.usage() == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "telemetry_complete": False,
        "known_tokens_only": True,
    }


def test_failed_and_cancelled_calls_keep_known_usage_but_mark_it_incomplete():
    diagnostics = RuleCallDiagnostics()
    response(diagnostics, usage=TOKENS)
    assert diagnostics.usage()["telemetry_complete"] is True
    diagnostics.on_chat_model_start({}, [])
    assert diagnostics.usage()["telemetry_complete"] is False
    diagnostics.on_llm_error(TimeoutError("private provider details"))
    snapshot = diagnostics.snapshot()
    assert snapshot["usage"]["total_tokens"] == 15
    assert snapshot["usage"]["telemetry_complete"] is False
    assert snapshot["model_attempts"] == 2
    assert snapshot["model_calls"] == 1
    assert snapshot["model_errors"] == [
        {"status": "error", "error": "TimeoutError", "http_status": None}
    ]
    assert "private provider details" not in json.dumps(snapshot)


@pytest.mark.parametrize("pending", [False, True])
def test_multiple_candidates_count_one_call_without_claiming_complete_usage(pending):
    diagnostics = RuleCallDiagnostics()
    diagnostics.on_chat_model_start({}, [], run_id="completed")
    if pending:
        diagnostics.on_chat_model_start({}, [], run_id="pending")
    diagnostics.on_llm_end(
        LLMResult(
            generations=[
                [
                    ChatGeneration(message=AIMessage(content="first", usage_metadata=TOKENS)),
                    ChatGeneration(message=AIMessage(content="second", usage_metadata=TOKENS)),
                ]
            ]
        ),
        run_id="completed",
    )
    snapshot = diagnostics.snapshot()
    assert snapshot["model_attempts"] == (2 if pending else 1)
    assert snapshot["model_calls"] == 1
    assert len(snapshot["response_shape"]) == 2
    assert snapshot["usage"]["telemetry_complete"] is False
    assert snapshot["usage"]["known_tokens_only"] is True
    # Candidate usage may duplicate request totals; neither sum nor first is authoritative.
    assert snapshot["usage"]["total_tokens"] == 0


def test_empty_completion_is_one_call_with_unknown_usage():
    diagnostics = RuleCallDiagnostics()
    diagnostics.on_chat_model_start({}, [], run_id="empty")
    diagnostics.on_llm_end(LLMResult(generations=[[]]), run_id="empty")
    snapshot = diagnostics.snapshot()
    assert snapshot["model_calls"] == 1
    assert snapshot["response_shape"] == []
    assert snapshot["usage"]["telemetry_complete"] is False


def test_ambiguous_candidate_usage_preserves_known_single_response_tokens():
    diagnostics = RuleCallDiagnostics()
    response(diagnostics, usage=TOKENS, run_id="known")
    diagnostics.on_chat_model_start({}, [], run_id="ambiguous")
    diagnostics.on_llm_end(
        LLMResult(
            generations=[
                [
                    ChatGeneration(message=AIMessage(content="first", usage_metadata=TOKENS)),
                    ChatGeneration(message=AIMessage(content="second", usage_metadata=TOKENS)),
                ]
            ]
        ),
        run_id="ambiguous",
    )
    snapshot = diagnostics.snapshot()
    assert snapshot["model_calls"] == 2
    assert snapshot["usage"]["total_tokens"] == 15
    assert snapshot["usage"]["telemetry_complete"] is False


def test_reports_keep_enums_without_free_text_or_invented_roles():
    diagnostics = RuleCallDiagnostics()
    response(
        diagnostics,
        usage=TOKENS,
        calls=[
            {
                "id": "report",
                "name": "SpecialistResult",
                "args": {
                    "role": "discovery",
                    "status": "needs_human",
                    "summary": "private text",
                    "limitations": ["private text"],
                    "task_id": "private text",
                },
            },
            {
                "id": "bad",
                "name": "SpecialistResult",
                "args": {"role": "invented", "status": "approved"},
            },
        ],
    )
    snapshot = diagnostics.snapshot()
    assert snapshot["specialist_reports"] == [
        {"role": "discovery", "status": "needs_human"},
        {"role": None, "status": None},
    ]
    assert "private text" not in json.dumps(snapshot)
    row = dict(outcome="model_run", passed=False, delegated_roles=["discovery"], **snapshot)
    diagnostic = stop_diagnostic(row)
    assert diagnostic["code"] == "specialist_stop_reported"
    assert diagnostic["semantic_root_cause"] == "not_established"
    assert diagnostic["reported_stops"] == [{"role": "discovery", "status": "needs_human"}]


def test_absent_report_is_not_inferred_from_last_role():
    row = dict(outcome="model_run", passed=False, delegated_roles=["discovery"])
    assert stop_diagnostic(row)["code"] == "incomplete_sequence_reason_unknown"


def test_response_shape_separates_empty_prose_and_malformed_calls():
    diagnostics = RuleCallDiagnostics()
    response(diagnostics, usage=TOKENS, run_id="run-1")
    response(diagnostics, usage=TOKENS, text="I cannot proceed.", run_id="run-2")
    response(
        diagnostics,
        usage=TOKENS,
        invalid=[{"type": "invalid_tool_call", "name": "execute_rule_analysis", "args": "{"}],
        run_id="run-3",
    )
    response(
        diagnostics,
        usage=TOKENS,
        calls=[{"name": "task", "args": {"subagent_type": "discovery"}, "id": "c1"}],
        agent="supervisor",
        run_id="run-4",
    )
    shapes = diagnostics.snapshot()["response_shape"]
    assert [
        (shape["agent"], shape["tool_names"], shape["invalid_tool_names"], shape["text_chars"])
        for shape in shapes
    ] == [
        ("supervisor", [], [], 0),
        ("supervisor", [], [], len("I cannot proceed.")),
        ("supervisor", [], ["execute_rule_analysis"], 0),
        ("supervisor", ["task"], [], 0),
    ]
    # The prose itself is never retained; only its length.
    assert "cannot proceed" not in json.dumps(diagnostics.snapshot())


def test_stop_diagnostic_keeps_the_shape_but_does_not_name_a_cause():
    diagnostics = RuleCallDiagnostics()
    response(diagnostics, usage=TOKENS, text="private reasoning", run_id="run-1")
    response(
        diagnostics,
        usage=TOKENS,
        invalid=[
            {"type": "invalid_tool_call", "name": "execute_rule_analysis", "args": "{"},
        ],
        run_id="run-2",
    )
    row = dict(
        outcome="model_run",
        passed=False,
        delegated_roles=["discovery"],
        **diagnostics.snapshot(),
    )
    diagnostic = stop_diagnostic(row)
    assert diagnostic["code"] == "incomplete_sequence_reason_unknown"
    assert diagnostic["responses_without_tool_calls"] == 2
    assert diagnostic["malformed_tool_call_names"] == ["execute_rule_analysis"]
    assert diagnostic["semantic_root_cause"] == "not_established"
    assert "private reasoning" not in json.dumps(diagnostic)


def test_subagent_responses_are_labelled_by_the_agent_that_emitted_them():
    diagnostics = RuleCallDiagnostics()
    response(diagnostics, usage=TOKENS, agent="discovery", run_id="run-a")
    response(diagnostics, usage=TOKENS, run_id="run-b")
    events = [event for event in diagnostics.events if event["status"] == "response"]
    assert [event["agent"] for event in events] == ["discovery", "supervisor"]
    assert [event["agent"] for event in diagnostics.events if event["status"] == "started"] == [
        "discovery",
        "supervisor",
    ]


def test_a_failed_call_does_not_label_the_next_response_with_a_stale_agent():
    diagnostics = RuleCallDiagnostics()
    diagnostics.on_chat_model_start({}, [], run_id="run-a", metadata={"lc_agent_name": "verifier"})
    diagnostics.on_llm_error(TimeoutError("private provider details"), run_id="run-a")
    response(diagnostics, usage=TOKENS, run_id="run-b")
    assert [event["agent"] for event in diagnostics.events if event["status"] == "response"] == [
        "supervisor"
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("I stopped after discovery.", "response_without_tool_call"),
        ("", "incomplete_sequence_reason_unknown"),
    ],
)
def test_a_prose_only_final_response_is_named_without_claiming_a_cause(text, expected):
    diagnostics = RuleCallDiagnostics()
    response(diagnostics, usage=TOKENS, text=text, run_id="run-a")
    row = dict(
        outcome="model_run",
        passed=False,
        delegated_roles=["discovery"],
        **diagnostics.snapshot(),
    )
    diagnostic = stop_diagnostic(row)
    assert diagnostic["code"] == expected
    assert diagnostic["semantic_root_cause"] == "not_established"


def test_a_trailing_tool_call_is_not_reported_as_a_prose_stop():
    diagnostics = RuleCallDiagnostics()
    response(diagnostics, usage=TOKENS, text="prose then a call", run_id="run-a")
    response(
        diagnostics,
        usage=TOKENS,
        calls=[{"name": "task", "args": {"subagent_type": "data-analyst"}, "id": "c2"}],
        run_id="run-b",
    )
    row = dict(
        outcome="model_run",
        passed=False,
        delegated_roles=["discovery"],
        **diagnostics.snapshot(),
    )
    assert stop_diagnostic(row)["code"] == "incomplete_sequence_reason_unknown"


def test_a_malformed_final_call_is_not_reported_as_a_prose_stop():
    diagnostics = RuleCallDiagnostics()
    response(
        diagnostics,
        usage=TOKENS,
        invalid=[{"type": "invalid_tool_call", "name": "execute_rule_analysis", "args": "{"}],
        run_id="run-a",
    )
    row = dict(
        outcome="model_run",
        passed=False,
        delegated_roles=["discovery"],
        **diagnostics.snapshot(),
    )
    diagnostic = stop_diagnostic(row)
    assert diagnostic["code"] == "incomplete_sequence_reason_unknown"
    assert diagnostic["malformed_tool_call_names"] == ["execute_rule_analysis"]


def test_a_reply_that_ignores_a_required_tool_is_named_as_such():
    """The harness sent ``tool_choice=required``; the reply still had no call.

    That is a different observation from a model that was free to answer and
    chose prose, so it gets its own stop code. The semantic cause stays open.
    """
    diagnostics = RuleCallDiagnostics()
    response(
        diagnostics,
        usage=TOKENS,
        text="discovery prose",
        agent="discovery",
        tool_choice="required",
        run_id="run-a",
    )
    row = dict(
        outcome="model_run",
        passed=False,
        delegated_roles=["discovery"],
        **diagnostics.snapshot(),
    )
    diagnostic = stop_diagnostic(row)
    assert diagnostic["code"] == "required_tool_call_not_honoured"
    assert diagnostic["responses_ignoring_required_tool"] == 1
    assert diagnostic["semantic_root_cause"] == "not_established"
    assert diagnostic["last_observed_role"] == "discovery"


def test_an_optional_request_that_answers_in_prose_keeps_the_weaker_code():
    diagnostics = RuleCallDiagnostics()
    response(diagnostics, usage=TOKENS, text="prose", tool_choice="auto", run_id="run-a")
    row = dict(
        outcome="model_run",
        passed=False,
        delegated_roles=["discovery"],
        **diagnostics.snapshot(),
    )
    diagnostic = stop_diagnostic(row)
    assert diagnostic["code"] == "response_without_tool_call"
    assert diagnostic["responses_ignoring_required_tool"] == 0


def test_a_honoured_required_tool_call_is_not_flagged():
    diagnostics = RuleCallDiagnostics()
    response(
        diagnostics,
        usage=TOKENS,
        calls=[{"name": "resolve_rule_request", "args": {}, "id": "c1"}],
        tool_choice="required",
        run_id="run-a",
    )
    row = dict(
        outcome="model_run",
        passed=False,
        delegated_roles=["discovery"],
        **diagnostics.snapshot(),
    )
    diagnostic = stop_diagnostic(row)
    assert diagnostic["responses_ignoring_required_tool"] == 0
    assert diagnostic["code"] == "incomplete_sequence_reason_unknown"


def test_the_required_tool_name_is_kept_when_the_request_named_one():
    diagnostics = RuleCallDiagnostics()
    response(
        diagnostics,
        usage=TOKENS,
        text="prose instead",
        tool_choice={"type": "function", "function": {"name": "SpecialistResult"}},
        run_id="run-a",
    )
    shape = diagnostics.snapshot()["response_shape"][0]
    assert shape["tool_choice"] == "named"
    assert shape["required_tool_names"] == ["SpecialistResult"]


def test_a_supervisor_prose_reply_after_an_ignored_requirement_keeps_the_strong_code():
    """The observed live shape: the subagent ignored ``required``, then the
    supervisor answered in prose with no requirement of its own.

    Reading only the final response would downgrade this to a free-form prose
    stop and lose the actual stopping event.
    """
    diagnostics = RuleCallDiagnostics()
    response(
        diagnostics,
        usage=TOKENS,
        calls=[{"name": "task", "args": {"subagent_type": "discovery"}, "id": "c1"}],
        run_id="run-a",
    )
    response(
        diagnostics,
        usage=TOKENS,
        calls=[{"name": "resolve_rule_request", "args": {}, "id": "c2"}],
        tool_choice="required",
        agent="discovery",
        run_id="run-b",
    )
    response(
        diagnostics,
        usage=TOKENS,
        text="discovery prose",
        tool_choice="required",
        agent="discovery",
        run_id="run-c",
    )
    response(diagnostics, usage=TOKENS, text="supervisor prose", run_id="run-d")
    row = dict(
        outcome="model_run",
        passed=False,
        delegated_roles=["discovery"],
        **diagnostics.snapshot(),
    )
    diagnostic = stop_diagnostic(row)
    assert diagnostic["code"] == "required_tool_call_not_honoured"
    assert diagnostic["responses_ignoring_required_tool"] == 1


def test_a_requirement_the_model_later_honoured_is_not_the_stop():
    """An ignored requirement followed by a real tool call did not stop anything."""
    diagnostics = RuleCallDiagnostics()
    response(diagnostics, usage=TOKENS, text="thinking", tool_choice="required", run_id="run-a")
    response(
        diagnostics,
        usage=TOKENS,
        calls=[{"name": "resolve_rule_request", "args": {}, "id": "c1"}],
        tool_choice="required",
        run_id="run-b",
    )
    response(diagnostics, usage=TOKENS, text="then I stopped", run_id="run-c")
    row = dict(
        outcome="model_run",
        passed=False,
        delegated_roles=["discovery"],
        **diagnostics.snapshot(),
    )
    diagnostic = stop_diagnostic(row)
    assert diagnostic["code"] == "response_without_tool_call"
    # The count still reports what was observed, even though it is not the stop.
    assert diagnostic["responses_ignoring_required_tool"] == 1


@pytest.mark.parametrize(
    "choice",
    [
        "SpecialistResult",
        {"type": "function", "function": {"name": "SpecialistResult"}},
        {"type": "function", "name": "SpecialistResult"},
        {"type": "tool", "name": "SpecialistResult"},
    ],
)
@pytest.mark.parametrize("called", [None, "resolve_rule_request", "SpecialistResult"])
def test_named_requirement_checks_the_actual_requested_tool(choice, called):
    diagnostics = RuleCallDiagnostics()
    calls = [{"name": called, "args": {}, "id": "c1"}] if called else []
    response(diagnostics, usage=TOKENS, tool_choice=choice, calls=calls, text="private prose")
    snapshot = diagnostics.snapshot()
    assert snapshot["response_shape"][0]["required_tool_names"] == ["SpecialistResult"]
    row = dict(outcome="model_run", passed=False, delegated_roles=[], **snapshot)
    expected = called != "SpecialistResult"
    assert stop_diagnostic(row)["responses_ignoring_required_tool"] == int(expected)
    assert (stop_diagnostic(row)["code"] == "required_tool_call_not_honoured") is expected
    assert "private prose" not in json.dumps(snapshot)


@pytest.mark.parametrize("choice", ["required", "SpecialistResult"])
def test_required_call_with_unparseable_arguments_is_not_an_absent_call(choice):
    diagnostics = RuleCallDiagnostics()
    response(
        diagnostics,
        usage=TOKENS,
        tool_choice=choice,
        invalid=[{"type": "invalid_tool_call", "name": "SpecialistResult", "args": "{"}],
    )
    row = dict(outcome="model_run", passed=False, delegated_roles=[], **diagnostics.snapshot())
    diagnostic = stop_diagnostic(row)
    assert diagnostic["responses_ignoring_required_tool"] == 0
    assert diagnostic["malformed_tool_call_names"] == ["SpecialistResult"]
    assert diagnostic["code"] == "incomplete_sequence_reason_unknown"


@pytest.mark.parametrize(
    "choice",
    [{"type": "auto"}, {"type": "none"}, {"type": "function", "function": "private"}, {}],
)
def test_optional_or_malformed_choice_does_not_create_a_named_requirement(choice):
    diagnostics = RuleCallDiagnostics()
    response(diagnostics, usage=TOKENS, tool_choice=choice, text="prose")
    row = dict(outcome="model_run", passed=False, delegated_roles=[], **diagnostics.snapshot())
    assert stop_diagnostic(row)["code"] == "response_without_tool_call"
    assert diagnostics.snapshot()["response_shape"][0]["required_tool_names"] == []
    assert "private" not in json.dumps(diagnostics.snapshot())


def test_all_generations_keep_their_request_metadata_after_interleaved_starts():
    diagnostics = RuleCallDiagnostics()
    diagnostics.on_chat_model_start(
        {},
        [],
        run_id="a",
        metadata={"lc_agent_name": "discovery"},
        invocation_params={"tool_choice": "required"},
    )
    diagnostics.on_chat_model_start(
        {},
        [],
        run_id="b",
        metadata={"lc_agent_name": "verifier"},
        invocation_params={"tool_choice": "SpecialistResult"},
    )
    batch = LLMResult(
        generations=[
            [
                ChatGeneration(message=AIMessage(content="first")),
                ChatGeneration(message=AIMessage(content="second")),
            ]
        ]
    )
    diagnostics.on_llm_end(batch, run_id="a")
    diagnostics.on_llm_end(batch, run_id="b")
    shapes = diagnostics.snapshot()["response_shape"]
    assert [(shape["agent"], shape["tool_choice"]) for shape in shapes] == [
        ("discovery", "required"),
        ("discovery", "required"),
        ("verifier", "named"),
        ("verifier", "named"),
    ]
    assert diagnostics._agents == {}
    assert diagnostics._tool_choice == {}


def test_exception_tokens_count_and_planned_denominator_survives_interruption():
    diagnostics = RuleCallDiagnostics()
    response(diagnostics, usage=TOKENS)
    rows = [
        dict(outcome="refused_by_parser", passed=False, model_calls=0),
        dict(outcome="supervisor_error", passed=False, **diagnostics.snapshot()),
    ]
    summary = summarize_rule_run(rows, planned_n=10)
    assert summary["n"] == 10
    assert summary["processed_n"] == 2
    assert summary["pending_n"] == 8
    assert summary["supervisor_error_n"] == 1
    assert summary["usage"]["total_tokens"] == 15
    assert summary["model_calls"] == 1
    assert summary["usage_telemetry_complete"] is True
    rows[-1]["usage"]["telemetry_complete"] = False
    assert summarize_rule_run(rows, planned_n=10)["usage_telemetry_complete"] is False


def runner_module():
    spec = importlib.util.spec_from_file_location(
        "rule_supervisor_runner_under_test",
        Path(__file__).resolve().parents[1] / "evals/run_stage10_rule_supervisor.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_precondition_guard_does_not_change_harness_registration():
    from generaldataagent.agents import factory

    before = factory._PROFILE_REGISTERED
    registration = factory.register_harness_profile
    runner_module()._assert_no_filesystem_tools()
    assert before == factory._PROFILE_REGISTERED
    assert factory.register_harness_profile is registration


@pytest.mark.parametrize("provider_status", [None, 429, 401])
def test_runner_persists_refusal_and_exception_with_full_denominator(
    tmp_path, monkeypatch, provider_status
):
    runner = runner_module()
    dataset = tmp_path / "data/stage6/dabstep"
    dataset.mkdir(parents=True)
    for name in ("manual.md", "payments-readme.md"):
        (dataset / name).write_text(
            "If a field is set to null it means that it applies to all possible "
            "values of that field."
        )
    (dataset / "merchant_data.json").write_text('[{"merchant": "Shop"}]')
    (dataset / "merchant_category_codes.csv").write_text("description,mcc\nshop,1\n")
    (dataset / "fees.json").write_text("[]")
    questions = [
        dict(task_id=str(i), level="hard", question=str(i), answer="unused") for i in range(10)
    ]
    (dataset / "dev.jsonl").write_text("\n".join(json.dumps(q) for q in questions))
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "DATASET", dataset)
    monkeypatch.setattr(runner, "digest", lambda path: "fixture-hash")
    model = SimpleNamespace(callbacks=[])
    monkeypatch.setattr(runner, "resolve_evaluation_model", lambda **kw: (None, {}))
    monkeypatch.setattr(runner, "build_evaluation_model", lambda *args: model)
    monkeypatch.setattr(runner, "build_csv_database", lambda *args: None)
    monkeypatch.setattr(runner, "add_json_tables", lambda *args: None)

    output = tmp_path / "run/report.json"

    def parse(question, **kwargs):
        if question == "0":
            raise runner.RuleQuestionError("rule_question_unrecognised_operation")
        stored = json.loads(output.read_text())
        assert stored["summary"]["n"] == 10
        assert stored["summary"]["processed_n"] == int(question)
        assert stored["role_controller_hash"] == "fixture-hash"
        assert stored["completion_hash"] == "fixture-hash"
        assert stored["document_context_hash"] == "fixture-hash"
        assert stored["governed_runner_hash"] == "fixture-hash"
        assert stored["document_extension"] == {"enabled": False}
        assert stored["document_configuration_hash"] is None
        return runner.parse_request

    from generaldataagent.contracts.rule_tools import RuleAnalysisRequest

    monkeypatch.setattr(
        runner,
        "parse_request",
        RuleAnalysisRequest(
            operation="select", semantics_excerpt="null", empty_list_is_wildcard=True
        ),
        raising=False,
    )
    monkeypatch.setattr(runner, "parse_rule_question", parse)

    class BrokenSupervisor:
        def __init__(self, **kwargs):
            pass

        async def arun(self, *args, **kwargs):
            if provider_status is None:
                response(model.callbacks[0], usage=TOKENS)
                raise ValueError("private exception details")
            error = RuntimeError("private provider details")
            error.status_code = provider_status
            model.callbacks[0].on_chat_model_start({}, [])
            model.callbacks[0].on_llm_error(error)
            raise error

    monkeypatch.setattr(runner, "RuleSupervisorRuntime", BrokenSupervisor)
    monkeypatch.setattr(
        sys,
        "argv",
        ["runner", "--output", str(output), "--model", "fixture", "--empty-list-is-wildcard"],
    )
    if provider_status is None:
        asyncio.run(runner.main())
    else:
        with pytest.raises(SystemExit) as blocked:
            asyncio.run(runner.main())
        assert blocked.value.code == 2
    stored = json.loads(output.read_text())
    summary = stored["summary"]
    assert summary["n"] == summary["processed_n"] == 10
    assert summary["refused_by_parser_n"] == 1
    expected_attempts = {None: 9, 429: 3, 401: 1}[provider_status]
    assert summary["supervisor_error_n"] == expected_attempts
    assert summary["attempted_n"] == expected_attempts
    assert summary["service_circuit_open_n"] == 9 - expected_attempts
    assert summary["usage"]["total_tokens"] == (9 * 15 if provider_status is None else 0)
    assert summary["passed_over_all_questions"] == 0
    assert stored["run_status"] == (
        "completed" if provider_status is None else "blocked_by_service"
    )
    if provider_status is not None:
        assert summary["usage_telemetry_complete"] is False
        assert all(
            row["attempted"] is False
            for row in stored["results"]
            if row["outcome"] == "service_circuit_open"
        )
    assert "private exception details" not in output.read_text()
    assert "private provider details" not in output.read_text()
    retained = output.parent / stored["retained_runtime"]["directory"]
    assert retained.is_dir()
    assert (retained / "state").is_file()
    assert len((retained / "checkpoint.key").read_bytes()) == 32
    assert (retained / "checkpoint.key").stat().st_mode & 0o777 == 0o600
    attempted = [row for row in stored["results"] if row["outcome"] == "supervisor_error"]
    assert all(row["rule_thread_id"] and row["supervisor_thread_id"] for row in attempted)
    assert all(row["diagnostic_events"][0]["status"] == "started" for row in attempted)
    assert all(row["request"]["operation"] == "select" for row in attempted)
    assert not list(output.parent.glob("work-*"))


@pytest.mark.parametrize("fail_after_checkpoint", [False, True])
def test_retained_runtime_can_reopen_encrypted_workflow_after_exit(tmp_path, fail_after_checkpoint):
    from generaldataagent.contracts import ActorContext
    from generaldataagent.contracts.rule_tools import RuleAnalysisRequest
    from generaldataagent.orchestration.checkpoints import open_protected_checkpointer
    from generaldataagent.orchestration.rule_workflow import (
        RuleWorkflowRuntime,
        RuleWorkflowServices,
    )

    runner = runner_module()
    actor = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("payments",))
    request = RuleAnalysisRequest(operation="select", semantics_excerpt="null means all")

    def sources():
        return {"annex": "sha256:" + "a" * 64}

    services = RuleWorkflowServices(
        monthly_context=lambda _: {"volume": "private-checkpoint-value"},
        match_rules=lambda _: {"ids": []},
        compute=lambda _: {},
        verify=lambda _: {"passed": True, "failures": []},
        deliver=lambda _: {},
    )
    output = tmp_path / "report.json"
    try:
        with runner._retained_runtime(output) as (folder, key):
            (folder / "payments.sqlite").write_bytes(b"synthetic retained source")
            with open_protected_checkpointer(folder / "state", encryption_key=key) as saver:
                flow = RuleWorkflowRuntime(
                    services=services,
                    checkpointer=saver,
                    source_hashes=sources,
                    workflow_version="retention-test",
                )
                thread, _ = flow.run(request, actor, interrupt_after=("monthly_context",))
            if fail_after_checkpoint:
                raise RuntimeError("synthetic runner interruption")
    except RuntimeError:
        assert fail_after_checkpoint
    key = (folder / "checkpoint.key").read_bytes()
    assert (folder / "payments.sqlite").read_bytes() == b"synthetic retained source"
    assert b"private-checkpoint-value" not in (folder / "state").read_bytes()
    with open_protected_checkpointer(folder / "state", encryption_key=key) as saver:
        flow = RuleWorkflowRuntime(
            services=services,
            checkpointer=saver,
            source_hashes=sources,
            workflow_version="retention-test",
        )
        assert flow.state_of(thread, request, actor)["monthly_context"]["volume"] == (
            "private-checkpoint-value"
        )
        assert flow.resume(thread, request, actor)["status"] == "completed"
    with pytest.raises(FileExistsError), runner._retained_runtime(output):
        pytest.fail("existing runtime must not be overwritten")


@pytest.mark.parametrize("limit", ["0", "-1", "11"])
def test_runner_rejects_invalid_denominator_before_model_access(tmp_path, monkeypatch, limit):
    runner = runner_module()
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "runner",
            "--output",
            str(tmp_path / "r.json"),
            "--model",
            "fixture",
            "--limit",
            limit,
            "--empty-list-is-wildcard",
        ],
    )
    with pytest.raises(ValueError, match="limit"):
        asyncio.run(runner.main())
