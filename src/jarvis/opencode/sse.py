"""Minimal Server-Sent Events parser.

httpx gives us lines; this converts them into typed events.  Multi-line
``data:`` fields are joined with newlines as required by the SSE spec.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class SSEEvent:
    event: str | None
    data: str
    id: str | None = None

    def json(self) -> dict[str, Any]:
        return json.loads(self.data)


async def iter_sse(lines: AsyncIterator[str]) -> AsyncIterator[SSEEvent]:
    event: str | None = None
    data: list[str] = []
    last_id: str | None = None

    async for raw_line in lines:
        line = raw_line.rstrip("\r")
        if not line:
            if data:
                yield SSEEvent(event=event, data="\n".join(data), id=last_id)
            event = None
            data = []
            last_id = None
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)
        elif field == "id":
            last_id = value

    if data:
        yield SSEEvent(event=event, data="\n".join(data), id=last_id)
