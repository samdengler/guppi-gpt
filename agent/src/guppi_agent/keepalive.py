"""Keepalive wrapper for AG-UI event streams.

CloudFront cuts an origin response after `origin response timeout` seconds of silence
(silence between packets, not total length). The agent therefore emits a CUSTOM event named
`ping` whenever the underlying stream has produced nothing for `interval` seconds. A data
line is used rather than an SSE comment because @ag-ui/client drops comment lines before
the subscriber sees them, and the page uses the ping to reset its own stall timer.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import TypeVar

from ag_ui.core import BaseEvent, CustomEvent, EventType

T = TypeVar("T", bound=BaseEvent)

PING_EVENT_NAME = "ping"
DEFAULT_PING_INTERVAL = 15.0


def ping_event() -> CustomEvent:
    return CustomEvent(type=EventType.CUSTOM, name=PING_EVENT_NAME, value={"t": time.time()})


async def with_keepalive(
    events: AsyncIterator[BaseEvent],
    interval: float = DEFAULT_PING_INTERVAL,
) -> AsyncIterator[BaseEvent]:
    """Yield every event from `events`, inserting a ping after `interval` seconds of silence."""
    queue: asyncio.Queue[BaseEvent | Exception | None] = asyncio.Queue()

    async def pump() -> None:
        try:
            async for event in events:
                await queue.put(event)
        except Exception as exc:  # surfaced to the consumer, which turns it into RUN_ERROR
            await queue.put(exc)
        finally:
            await queue.put(None)

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=interval)
            except TimeoutError:
                yield ping_event()
                continue
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        task.cancel()
