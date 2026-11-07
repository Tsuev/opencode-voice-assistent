"""Always-on voice loop: wake word -> record -> STT -> OpenCode -> TTS.

Audio runs in a worker thread because PortAudio reads are blocking.  Anything
that touches asyncio (state, events, OpenCode) is dispatched back to the main
loop with ``run_coroutine_threadsafe``.

P1 is intentionally half-duplex: the microphone stream is closed while Jarvis
speaks, which prevents the assistant from hearing itself.  True barge-in and
echo cancellation are P2 work.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections import deque

import numpy as np
from rich.console import Console

from .audio.input import Microphone
from .audio.vad import WebRtcVad
from .config import JarvisConfig
from .events import Event, EventType
from .runtime import Runtime
from .speech.policy import strip_wake_word
from .state import AssistantState
from .stt.whisper import WhisperSTT
from .wake.base import WakeDetector

log = logging.getLogger(__name__)

_STOP_WORDS = {
    "стоп",
    "отмена",
    "хватит",
    "пока",
    "до свидания",
    "stop",
    "cancel",
    "goodbye",
}
_WAKE_WORDS = [
    "hey jarvis",
    "хей джарвис",
    "эй джарвис",
    "окей джарвис",
    "джарвис",
    "jarvis",
]


class VoiceLoop:
    def __init__(
        self,
        runtime: Runtime,
        detector: WakeDetector,
        stt: WhisperSTT,
        config: JarvisConfig,
        *,
        console: Console | None = None,
        speak: bool = True,
        beep: bool = True,
        microphone: object | None = None,
    ) -> None:
        self.runtime = runtime
        self.detector = detector
        self.stt = stt
        self.config = config
        self.console = console
        self.speak = speak
        self.beep = beep
        self.microphone = microphone or Microphone(
            config.audio.input_device,
            sample_rate=16000,
            block_ms=config.audio.block_ms,
        )
        self.vad = WebRtcVad(mode=config.vad.mode)
        self.stop_event = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pre_roll: list[np.ndarray] = []

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        self._loop = loop
        await self.runtime.set_listening(True)
        await self.runtime.bus.emit(EventType.LOG, message="voice loop started")
        done = asyncio.Event()

        def worker() -> None:
            try:
                self._run_blocking()
            except Exception as exc:  # noqa: BLE001
                log.exception("voice worker crashed")
                self._emit(EventType.ERROR, message=str(exc))
            finally:
                loop.call_soon_threadsafe(done.set)

        # A daemon thread lets the process exit even if PortAudio is wedged in
        # a blocking read (a real problem after a broken CoreAudio/WASAPI state).
        thread = threading.Thread(target=worker, name="jarvis-voice-loop", daemon=True)
        thread.start()
        try:
            await done.wait()
        finally:
            self.stop()
            thread.join(timeout=2.0)
            await self.runtime.set_listening(False)
            await self.runtime.bus.emit(EventType.LOG, message="voice loop stopped")

    def stop(self) -> None:
        self.stop_event.set()

    # ------------------------------------------------------------------
    # worker thread
    # ------------------------------------------------------------------
    def _run_blocking(self) -> None:
        while not self.stop_event.is_set():
            self._set_state(AssistantState.LISTENING, error=None)
            try:
                with self.microphone.frames_stream() as frames:
                    if not self._wait_for_wake(frames):
                        break
                    pre_roll = self._pre_roll
                    audio = self._record_command(
                        frames,
                        pre_roll,
                        grace_ms=self.config.vad.wake_grace_ms,
                        timeout=None,
                    )

                if audio is None:
                    self._speak("Не расслышал.")
                    continue
                if not self._handle_audio(audio):
                    continue
                self._conversation_mode()
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                log.error("voice loop error: %s", exc)
                self._emit(EventType.ERROR, message=str(exc))
                self._set_state(AssistantState.ERROR, error=str(exc))
                self.stop_event.wait(0.8)
                if not self.stop_event.is_set():
                    self._set_state(AssistantState.LISTENING, error=None)

    def _wait_for_wake(self, frames) -> bool:
        pre_max = max(1, int(self.config.vad.pre_roll_ms / self.config.audio.block_ms))
        pre_roll: deque[np.ndarray] = deque(maxlen=pre_max)
        for frame in frames:
            if self.stop_event.is_set():
                return False
            pre_roll.append(frame)
            if self.detector.process(frame):
                self._pre_roll = list(pre_roll)
                self._emit(
                    EventType.WAKE,
                    engine=self.detector.name,
                    phrase=self.config.wake.phrase,
                    score=self.detector.last_score,
                )
                self._set_state(AssistantState.LISTENING)
                if self.beep:
                    self._play_beep()
                return True
        return False

    def _record_command(
        self,
        frames,
        pre_roll: list[np.ndarray],
        *,
        grace_ms: int = 0,
        timeout: float | None = None,
    ) -> np.ndarray | None:
        block_ms = self.config.audio.block_ms
        collected: list[np.ndarray] = list(pre_roll)
        speech = any(self.vad.is_speech(frame) for frame in pre_roll)
        speech_run = 0
        silence_ms = 0
        elapsed_ms = 0
        started = time.monotonic()
        pre_max = max(1, int(self.config.vad.pre_roll_ms / block_ms))

        for frame in frames:
            if self.stop_event.is_set():
                return None
            elapsed_ms += block_ms
            is_speech = self.vad.is_speech(frame)

            if not speech:
                collected.append(frame)
                if len(collected) > pre_max:
                    collected = collected[-pre_max:]
                speech_run = speech_run + 1 if is_speech else 0
                if speech_run * block_ms >= self.config.vad.min_speech_ms:
                    speech = True
                    silence_ms = 0
                if timeout is not None and time.monotonic() - started > timeout:
                    return None
                continue

            collected.append(frame)
            silence_ms = 0 if is_speech else silence_ms + block_ms
            if silence_ms >= self.config.vad.silence_ms and elapsed_ms >= grace_ms:
                break
            if elapsed_ms >= self.config.vad.max_command_s * 1000:
                break

        if not speech:
            return None
        return np.concatenate(collected)

    def _conversation_mode(self) -> None:
        timeout = self.config.speech.conversation_timeout
        if timeout <= 0:
            return
        deadline = time.monotonic() + timeout
        while not self.stop_event.is_set() and time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            try:
                with self.microphone.frames_stream() as frames:
                    audio = self._record_command(frames, [], grace_ms=0, timeout=remaining)
            except Exception:  # noqa: BLE001 - device may be busy
                log.debug("conversation mode capture failed", exc_info=True)
                return
            if audio is None:
                return
            if not self._handle_audio(audio, conversation=True):
                return
            deadline = time.monotonic() + timeout

    # ------------------------------------------------------------------
    # turn handling
    # ------------------------------------------------------------------
    def _handle_audio(self, audio: np.ndarray, *, conversation: bool = False) -> bool:
        self._set_state(AssistantState.TRANSCRIBING)
        text = self._call(self.stt.transcribe(audio, 16000)).strip()
        if not text:
            if not conversation:
                self._speak("Не расслышал.")
            return False

        command = strip_wake_word(text, _WAKE_WORDS).strip(" ,.!?-—")
        if not command:
            self._speak("Слушаю.")
            return False
        if command.lower().strip(" .!?") in _STOP_WORDS:
            self._speak("До связи.")
            return False

        self._set_state(AssistantState.THINKING)
        try:
            self._call(self.runtime.ask(command, speak=self.speak))
        except Exception as exc:  # noqa: BLE001
            log.error("ask failed: %s", exc)
            self._emit(EventType.ERROR, message=str(exc))
            self._set_state(AssistantState.ERROR, error=str(exc))
            return False
        return True

    # ------------------------------------------------------------------
    # thread -> asyncio bridge
    # ------------------------------------------------------------------
    def _call(self, coro):
        loop = self._loop
        if loop is None or loop.is_closed():
            raise RuntimeError("Главный event loop недоступен")
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    def _emit(self, event_type: EventType, **data) -> None:
        self.runtime.bus.publish_threadsafe(Event(event_type, data))

    def _set_state(self, state: AssistantState, **fields) -> None:
        with contextlib.suppress(Exception):
            self._call(self.runtime.state.set(state, **fields))

    def _speak(self, text: str) -> None:
        with contextlib.suppress(Exception):
            self._call(self.runtime.speak(text))

    def _play_beep(self) -> None:
        try:
            import sounddevice as sd

            samples = np.sin(2 * np.pi * 880 * np.arange(0, 0.1, 1 / 16000)).astype(np.float32)
            sd.play(samples * 0.08, 16000)
        except Exception:  # noqa: BLE001
            pass
