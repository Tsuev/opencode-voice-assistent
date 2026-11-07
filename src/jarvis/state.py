"""Canonical assistant state shared by CLI, API and the future GUI."""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from .events import EventBus, EventType


class AssistantState(StrEnum):
    OFFLINE = "offline"
    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    SPEAKING = "speaking"
    PAUSED = "paused"
    ERROR = "error"


class StateSnapshot(BaseModel):
    state: AssistantState = AssistantState.OFFLINE
    wake_enabled: bool = True
    session_id: str | None = None
    last_transcript: str | None = None
    last_reply: str | None = None
    error: str | None = None
    updated_at: float = Field(default_factory=time.time)


class StateStore:
    """Small mutable snapshot that publishes ``state`` events on change."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._snapshot = StateSnapshot()

    def get(self) -> StateSnapshot:
        return self._snapshot.model_copy(deep=True)

    async def set(self, state: AssistantState, **fields: Any) -> StateSnapshot:
        self._snapshot = self._snapshot.model_copy(
            update={"state": state, "updated_at": time.time(), **fields}
        )
        await self._bus.emit(EventType.STATE, **self._snapshot.model_dump(mode="json"))
        return self.get()

    async def update(self, **fields: Any) -> StateSnapshot:
        self._snapshot = self._snapshot.model_copy(update={**fields, "updated_at": time.time()})
        await self._bus.emit(EventType.STATE, **self._snapshot.model_dump(mode="json"))
        return self.get()
