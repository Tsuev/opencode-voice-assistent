"""In-process event bus.

This is the seam between the audio/agent core and every frontend:

* the CLI subscribes for pretty output,
* ``jarvis serve`` re-publishes events over SSE for the future GUI,
* tests can subscribe without touching audio hardware.

Publishers run on the asyncio loop.  Worker threads use
:meth:`EventBus.publish_threadsafe`.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    STATE = "state"
    WAKE = "wake"
    TRANSCRIPT = "transcript"
    ASSISTANT_DELTA = "assistant.delta"
    ASSISTANT_TEXT = "assistant.text"
    TOOL = "tool"
    PERMISSION = "permission"
    SPEECH_START = "speech.start"
    SPEECH_END = "speech.end"
    ERROR = "error"
    LOG = "log"


@dataclass(slots=True)
class Event:
    type: EventType
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"type": str(self.type), "ts": self.ts, "data": self.data}


_CLOSE = object()


class Subscription(AsyncIterator[Event]):
    """Bounded per-subscriber queue.

    Slow consumers lose the oldest events instead of blocking the core.
    """

    def __init__(self, queue: asyncio.Queue[Event | object]) -> None:
        self._queue = queue
        self._closed = False

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> Event:
        if self._closed:
            raise StopAsyncIteration
        item = await self._queue.get()
        if item is _CLOSE:
            self._closed = True
            raise StopAsyncIteration
        assert isinstance(item, Event)
        return item

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put_nowait(_CLOSE)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(_CLOSE)


class EventBus:
    def __init__(self, max_queue: int = 256) -> None:
        self._max_queue = max_queue
        self._subscribers: set[asyncio.Queue[Event | object]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> Subscription:
        queue: asyncio.Queue[Event | object] = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.add(queue)
        return Subscription(queue)

    def unsubscribe(self, subscription: Subscription) -> None:
        self._subscribers.discard(subscription._queue)  # noqa: SLF001 - intentional

    async def publish(self, event: Event) -> None:
        for queue in list(self._subscribers):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)

    async def emit(self, event_type: EventType, **data: Any) -> None:
        await self.publish(Event(event_type, data))

    def publish_threadsafe(self, event: Event) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.publish(event)))
