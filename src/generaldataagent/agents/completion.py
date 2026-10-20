"""Finish a governed operation once its validated delivery is available."""

import asyncio
from collections.abc import Awaitable
from typing import Any


async def await_validated_completion(operation: Awaitable, completed: asyncio.Event) -> Any:
    """The caller alone may signal successful validation; cancellation is always cleaned up."""
    work = asyncio.ensure_future(operation)
    signal = asyncio.create_task(completed.wait())
    try:
        await asyncio.wait({work, signal}, return_when=asyncio.FIRST_COMPLETED)
        if completed.is_set():
            return {}
        return await work
    finally:
        for task in (work, signal):
            if not task.done():
                task.cancel()
        await asyncio.gather(work, signal, return_exceptions=True)
