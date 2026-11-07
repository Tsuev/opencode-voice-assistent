"""Jarvis runtime: the frontend-independent core.

Everything a GUI needs is exposed here and through :mod:`jarvis.api`:

* state transitions (:class:`jarvis.state.StateStore`)
* a typed event stream (:class:`jarvis.events.EventBus`)
* ``ask`` / ``speak`` / ``listen`` commands

The CLI and the HTTP API are thin adapters over this class.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import Any

from rich.console import Console

from .config import JarvisConfig
from .events import EventBus, EventType
from .opencode.client import OpencodeClient, OpencodeError
from .paths import AppPaths
from .speech.policy import prepare
from .speech.streaming import SpeechStreamer
from .state import AssistantState, StateStore
from .tts.router import TTSRouter

log = logging.getLogger(__name__)


class _TurnCollector:
    """Collect streamed assistant text parts in order."""

    def __init__(self) -> None:
        self._parts: dict[tuple[str, int], str] = {}
        self._order: list[tuple[str, int]] = []

    def _key(self, message_id: str, ordinal: int) -> tuple[str, int]:
        key = (message_id, ordinal)
        if key not in self._parts:
            self._parts[key] = ""
            self._order.append(key)
        return key

    def delta(self, message_id: str, ordinal: int, delta: str) -> str:
        key = self._key(message_id, ordinal)
        self._parts[key] += delta
        return self._parts[key]

    def ended(self, message_id: str, ordinal: int, text: str) -> None:
        key = self._key(message_id, ordinal)
        self._parts[key] = text

    @property
    def text(self) -> str:
        parts = (self._parts[key] for key in self._order)
        return " ".join(part.strip() for part in parts if part.strip())


class Runtime:
    def __init__(
        self,
        config: JarvisConfig,
        paths: AppPaths,
        *,
        console: Console | None = None,
    ) -> None:
        self.config = config
        self.paths = paths
        self.console = console
        self.bus = EventBus()
        self.state = StateStore(self.bus)
        self.tts = TTSRouter(
            config.tts,
            paths,
            console=console,
            output_device=config.audio.output_device,
        )
        self._client: OpencodeClient | None = None
        self._session_id: str | None = None
        self._turn_lock = asyncio.Lock()
        self._speak_lock = asyncio.Lock()
        self._listening = True

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        self.bus.bind_loop(asyncio.get_running_loop())
        await self.state.set(AssistantState.IDLE, wake_enabled=True)
        await self.bus.emit(EventType.LOG, level="info", message="Jarvis runtime started")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
        await self.tts.close()
        await self.state.set(AssistantState.OFFLINE)

    async def client(self) -> OpencodeClient:
        if self._client is None:
            self._client = OpencodeClient(self.config.opencode, console=self.console)
            await self._client.start()
        return self._client

    # ------------------------------------------------------------------
    # OpenCode session
    # ------------------------------------------------------------------
    async def ensure_session(self, *, force_new: bool = False) -> str:
        if force_new:
            self._session_id = None
        if self._session_id:
            return self._session_id
        client = await self.client()
        agent = self.config.opencode.agent or None
        model = self.config.opencode.model or None
        try:
            session_id = await client.create_session(
                title="Jarvis voice",
                directory=self.config.opencode.directory or Path.cwd(),
                agent=agent,
                model=model,
            )
        except OpencodeError as exc:
            if agent and "agent" in str(exc).lower():
                # A missing agent should not block a first run; retry once
                # without it and let ``jarvis doctor`` explain the fix.
                log.warning("agent %r unavailable, retrying without it", agent)
                session_id = await client.create_session(
                    title="Jarvis voice",
                    directory=self.config.opencode.directory or Path.cwd(),
                    model=model,
                )
            else:
                raise
        self._session_id = session_id
        await self.state.update(session_id=session_id)
        await self.bus.emit(
            EventType.LOG, message="OpenCode session created", session_id=session_id
        )
        return session_id

    async def new_session(self) -> str:
        return await self.ensure_session(force_new=True)

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------
    async def ask(self, text: str, *, speak: bool = False) -> str:
        text = text.strip()
        if not text:
            raise ValueError("Пустой запрос")
        async with self._turn_lock:
            await self.state.set(AssistantState.THINKING, last_transcript=text)
            await self.bus.emit(EventType.TRANSCRIPT, text=text, final=True)
            client = await self.client()
            session_id = await self.ensure_session()
            collector = _TurnCollector()
            speaker = SpeechStreamer(self.speak) if speak else None
            error: str | None = None

            try:
                async for payload in client.stream_turn(session_id, text):
                    await self._handle_turn_event(payload, collector, speaker)
                    if payload.get("type") == "session.execution.failed":
                        data = payload.get("data") or {}
                        error = str(
                            data.get("error") or data.get("message") or "OpenCode execution failed"
                        )
            except OpencodeError as exc:
                error = str(exc)
                await self.bus.emit(EventType.ERROR, message=error)
                await self.state.set(AssistantState.ERROR, error=error)
                if speaker is not None:
                    await speaker.finish()
                raise

            reply = collector.text.strip()
            await self.state.update(last_reply=reply)

            tts_error: Exception | None = None
            if speaker is not None:
                tts_error = await speaker.finish()
                if tts_error is not None:
                    await self.bus.emit(EventType.ERROR, message=str(tts_error))

            if error:
                await self.bus.emit(EventType.ERROR, message=error)
                await self.state.set(AssistantState.ERROR, error=error)
                return reply

            if speaker is None or speaker.spoken_chars == 0:
                await self.state.set(AssistantState.IDLE)
            return reply

    async def _handle_turn_event(
        self,
        payload: dict[str, Any],
        collector: _TurnCollector,
        speaker: SpeechStreamer | None = None,
    ) -> None:
        event_type = str(payload.get("type", ""))
        data = payload.get("data") or {}
        session_id = data.get("sessionID")
        message_id = str(data.get("assistantMessageID") or data.get("messageID") or "")
        ordinal = int(data.get("ordinal") or 0)

        if event_type == "session.text.delta":
            delta = str(data.get("delta") or "")
            if delta:
                collector.delta(message_id, ordinal, delta)
                if speaker is not None:
                    speaker.feed_delta(delta)
                await self.bus.emit(
                    EventType.ASSISTANT_DELTA,
                    text=delta,
                    session_id=session_id,
                    ordinal=ordinal,
                )
        elif event_type == "session.text.ended":
            full = str(data.get("text") or "")
            collector.ended(message_id, ordinal, full)
            if speaker is not None:
                speaker.end_part(full)
            await self.bus.emit(
                EventType.ASSISTANT_TEXT,
                text=full,
                session_id=session_id,
                ordinal=ordinal,
            )
        elif event_type.startswith("session.tool."):
            status = event_type.rsplit(".", 1)[-1]
            await self.bus.emit(
                EventType.TOOL,
                name=data.get("name") or data.get("tool") or "tool",
                status=status,
                session_id=session_id,
                input=data.get("input"),
            )
        elif event_type in {"permission.asked", "permission.replied"}:
            await self.bus.emit(
                EventType.PERMISSION,
                status=event_type.rsplit(".", 1)[-1],
                session_id=session_id,
                **{k: v for k, v in data.items() if k != "sessionID"},
            )

    async def speak(
        self, text: str, *, language: str | None = None, speed: float | None = None
    ) -> None:
        from .speech.policy import TRUNCATION_NOTE

        resolved = language if language in {"ru", "en"} else self.config.language
        note = "Details in the terminal." if resolved == "en" else TRUNCATION_NOTE
        plan = prepare(
            text,
            max_chars=self.config.speech.max_chars,
            max_sentence_chars=self.config.speech.max_sentence_chars,
            truncation_note=note,
        )
        if not plan.chunks:
            await self.bus.emit(EventType.SPEECH_END, text="", dropped=True)
            return

        spoken = " ".join(plan.chunks)
        async with self._speak_lock:
            await self.state.set(AssistantState.SPEAKING)
            await self.bus.emit(EventType.SPEECH_START, text=spoken, truncated=plan.truncated)
            try:
                # Synthesize and play sentence by sentence instead of joining
                # the whole reply into one blocking request.  The first
                # sentence starts playing as soon as it is ready.
                from .audio.output import play_audio

                for chunk in plan.chunks:
                    started = time.perf_counter()
                    audio = await self.tts.synthesize(
                        chunk, language=resolved, speed=speed
                    )
                    log.info(
                        "TTS %.2f c for %d chars (%s)",
                        time.perf_counter() - started,
                        len(chunk),
                        resolved,
                    )
                    if not audio.empty:
                        await asyncio.to_thread(
                            play_audio, audio, self.tts.output_device
                        )
            finally:
                await self.bus.emit(EventType.SPEECH_END, text=spoken)
                await self.state.set(AssistantState.IDLE)

    async def stop_speaking(self) -> None:
        from .audio.output import stop_audio

        await asyncio.to_thread(stop_audio)
        if self._client and self._session_id:
            with contextlib.suppress(OpencodeError):
                await self._client.interrupt(self._session_id)
        await self.state.set(AssistantState.IDLE)

    async def set_listening(self, enabled: bool) -> None:
        self._listening = enabled
        await self.state.set(
            AssistantState.LISTENING if enabled else AssistantState.PAUSED,
            wake_enabled=enabled,
        )
        await self.bus.emit(EventType.LOG, message=f"listening={enabled}")

    # ------------------------------------------------------------------
    # introspection for GUI
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.get().model_dump(mode="json"),
            "language": self.config.language,
            "config": {
                "wake": self.config.wake.model_dump(mode="json"),
                "stt": self.config.stt.model_dump(mode="json"),
                "tts": self.config.tts.model_dump(mode="json"),
                "speech": self.config.speech.model_dump(mode="json"),
                "opencode": {
                    key: value
                    for key, value in self.config.opencode.model_dump(mode="json").items()
                    if key != "binary"
                },
            },
            "session_id": self._session_id,
            "listening": self._listening,
        }
