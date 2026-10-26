"""Bounded continuation uses observed tool progress, never model completion claims."""

import asyncio
import json

import pytest
from langchain_core.messages import ToolMessage

from generaldataagent.agents.supervision import continue_role_sequence

SEQUENCE = ("discovery", "data-analyst", "verifier", "reporter")


def drive(script, *, allowed=True):
    events, states = [], []
    config = {"configurable": {"thread_id": "same-thread"}, "recursion_limit": 40}

    class Graph:
        async def ainvoke(self, state, actual):
            assert actual is config
            states.append(state)
            return script(events, len(states))

    result = asyncio.run(
        continue_role_sequence(
            Graph(),
            {"messages": [{"role": "user", "content": "original"}]},
            config,
            events=events,
            sequence=SEQUENCE,
            completed=asyncio.Event(),
            can_continue=lambda: allowed,
        )
    )
    return result, events, states


def test_prose_stop_after_each_role_gets_a_bounded_next_stage():
    def script(events, turn):
        events.append(SEQUENCE[turn - 1])
        return {"messages": []}

    _, events, states = drive(script)
    assert events == list(SEQUENCE[:-1])
    assert len(states) == 3
    assert "Continue with data-analyst" in states[1]["messages"][0]["content"]
    assert "Continue with verifier" in states[2]["messages"][0]["content"]


def test_no_tool_progress_does_not_earn_another_call():
    result, events, states = drive(
        lambda events, turn: {"structured_response": {"status": "completed"}}
    )
    assert events == [] and len(states) == 1
    assert result["structured_response"]["status"] == "completed"


def test_repeating_a_completed_role_stops_instead_of_spending_more_budget():
    def script(events, turn):
        if not events:
            events.append("discovery")
        return {}

    _, events, states = drive(script)
    assert events == ["discovery"] and len(states) == 2


@pytest.mark.parametrize("status", ["failed", "needs_human"])
@pytest.mark.parametrize("where", ["supervisor", "specialist"])
def test_explicit_failure_and_human_review_are_never_retried(status, where):
    def script(events, turn):
        events.append("discovery")
        proposal = {"role": "discovery", "status": status, "summary": "Stop here"}
        if where == "supervisor":
            return {"structured_response": proposal}
        return {
            "messages": [ToolMessage(name="task", tool_call_id="t", content=json.dumps(proposal))]
        }

    _, _, states = drive(script)
    assert len(states) == 1


@pytest.mark.parametrize(
    "result",
    [
        {"__interrupt__": ["review"]},
        {
            "messages": [
                ToolMessage(name="task", tool_call_id="t", content="denied", status="error")
            ]
        },
    ],
)
def test_interrupts_and_task_errors_keep_their_original_stop(result):
    def script(events, turn):
        events.append("discovery")
        return result

    actual, _, states = drive(script)
    assert actual == result and len(states) == 1


def test_failed_candidate_prevents_continuation():
    def script(events, turn):
        events.extend(SEQUENCE[:2])
        return {}

    _, _, states = drive(script, allowed=False)
    assert len(states) == 1


def test_total_deadline_cancels_a_continuation_call():
    async def scenario():
        events, cancelled = [], []

        class Graph:
            async def ainvoke(self, state, config):
                if not events:
                    events.append("discovery")
                    return {}
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.append(True)

        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await continue_role_sequence(
                    Graph(),
                    {},
                    {},
                    events=events,
                    sequence=SEQUENCE,
                    completed=asyncio.Event(),
                    can_continue=lambda: True,
                )
        assert cancelled == [True]

    asyncio.run(scenario())
