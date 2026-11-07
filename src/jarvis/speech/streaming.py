"""Incremental speech for streamed assistant text.

The whole point of the voice assistant is that the acknowledgment («Открываю
Steam») is spoken **before** the tool runs.  Collecting the full turn and
speaking it at the end would break that, so deltas are split into sentences and
fed to a background TTS queue while the agent keeps working.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


class SpeechStreamer:
    def __init__(self, speak: Callable[[str], Awaitable[None]]) -> None:
        self._speak = speak
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._buffer = ""
        self._part_text = ""
        self._saw_delta = False
        self.error: Exception | None = None
        self.spoken_chars = 0
        self._task = asyncio.create_task(self._run(), name="jarvis-tts-stream")

    def feed_delta(self, delta: str) -> None:
        if not delta:
            return
        self._saw_delta = True
        self._part_text += delta
        self._buffer += delta
        self._flush_complete()

    def end_part(self, full_text: str) -> None:
        if self._saw_delta:
            self._flush_all()
            # Defensive: some providers stream partial deltas and send the
            # complete text only in ``text.ended``.
            if full_text.startswith(self._part_text) and len(full_text) > len(self._part_text):
                self._enqueue(full_text[len(self._part_text) :])
        elif full_text.strip():
            self._enqueue(full_text)
        self._saw_delta = False
        self._part_text = ""
        self._buffer = ""

    def _flush_complete(self) -> None:
        parts = _SENTENCE_END.split(self._buffer)
        if len(parts) <= 1:
            return
        for part in parts[:-1]:
            self._enqueue(part)
        self._buffer = parts[-1]

    def _flush_all(self) -> None:
        if self._buffer.strip():
            self._enqueue(self._buffer)
        self._buffer = ""

    def _enqueue(self, text: str) -> None:
        text = text.strip()
        if text:
            self._queue.put_nowait(text)

    async def finish(self) -> Exception | None:
        self._flush_all()
        await self._queue.put(None)
        await self._task
        return self.error

    async def _run(self) -> None:
        while True:
            text = await self._queue.get()
            if text is None:
                return
            if self.error is not None:
                continue
            try:
                await self._speak(text)
                self.spoken_chars += len(text)
            except Exception as exc:  # noqa: BLE001 - report once, keep draining
                self.error = exc
