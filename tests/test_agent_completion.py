"""Validated completion must stop surplus model work, while failures still propagate."""

import asyncio

import pytest

from generaldataagent.agents.completion import await_validated_completion


def test_delivery_event_cancels_surplus_work_without_waiting_for_a_final_model_reply():
    async def run():
        event, cancelled = asyncio.Event(), asyncio.Event()

        async def graph():
            try:
                event.set()
                await asyncio.sleep(30)
            finally:
                cancelled.set()

        async with asyncio.timeout(0.2):
            result = await await_validated_completion(graph(), event)
        assert result == {} and cancelled.is_set()

    asyncio.run(run())


def test_graph_result_and_failure_are_preserved_without_a_validated_delivery():
    async def run():
        async def success():
            return {"structured_response": None}

        async def failure():
            raise PermissionError("scope denied")

        assert await await_validated_completion(success(), asyncio.Event()) == {
            "structured_response": None
        }
        with pytest.raises(PermissionError):
            await await_validated_completion(failure(), asyncio.Event())

    asyncio.run(run())


def test_whole_workflow_deadline_cancels_graph_and_leaves_no_background_operation():
    async def run():
        cancelled = asyncio.Event()

        async def graph():
            try:
                await asyncio.sleep(30)
            finally:
                cancelled.set()

        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.01):
                await await_validated_completion(graph(), asyncio.Event())
        assert cancelled.is_set()

    asyncio.run(run())
