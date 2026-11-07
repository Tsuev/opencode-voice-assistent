"""Language-agnostic wake word: VAD segmentation + faster-whisper.

openWakeWord's bundled model is English ("hey jarvis"), so it does not hear the
Russian "Джарвис" reliably.  This detector segments speech with WebRTC VAD and
transcribes each short segment with a tiny Whisper model.  It is heavier than a
dedicated keyword spotter, but it works out of the box on Windows and macOS and
understands any phrase we put in the config.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

import numpy as np

from ..audio.vad import WebRtcVad
from ..speech.policy import contains_wake_word

log = logging.getLogger(__name__)

DEFAULT_SAMPLE_RATE = 16000
DEFAULT_BLOCK_MS = 80


def _default_phrases(phrase: str) -> list[str]:
    phrases = ["джарвис", "эй джарвис", "hey jarvis", "jarvis"]
    if phrase and phrase.strip():
        phrases.insert(0, phrase.strip())
    # Preserve order, drop duplicates.
    return list(dict.fromkeys(phrases))


class VadWhisperDetector:
    name = "vad-whisper"
    frame_size = 1280  # nominal 80 ms at 16 kHz; process() accepts any block

    def __init__(
        self,
        *,
        phrase: str = "джарвис",
        phrases: list[str] | None = None,
        model: str = "tiny",
        language: str = "ru",
        device: str = "cpu",
        compute_type: str = "int8",
        cache_dir: Path | None = None,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        vad_mode: int = 2,
        pre_roll_ms: int = 400,
        silence_ms: int = 600,
        min_speech_ms: int = 160,
        max_segment_s: float = 8.0,
        debounce: float = 1.2,
        threshold: float = 0.5,
        vad: object | None = None,
        transcriber: Callable[[np.ndarray], str] | None = None,
        console=None,
    ) -> None:
        self.phrase = phrase
        self.phrases = phrases or _default_phrases(phrase)
        self.model = model
        self.language = language if language in {"ru", "en"} else "ru"
        self.device = device
        self.compute_type = compute_type
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.sample_rate = sample_rate
        self.pre_roll_ms = pre_roll_ms
        self.silence_ms = silence_ms
        self.min_speech_ms = min_speech_ms
        self.max_segment_s = max_segment_s
        self.debounce = debounce
        self.threshold = threshold  # kept for the wake-test UI
        self.console = console
        self.last_score = 0.0
        self.last_text = ""
        self.last_audio = np.zeros(0, dtype=np.int16)

        self._vad = vad or WebRtcVad(mode=vad_mode, sample_rate=sample_rate)
        self._transcriber = transcriber
        self._model = None
        self._model_lock = threading.Lock()

        self._block_ms = DEFAULT_BLOCK_MS
        self._block_known = False
        self._pre_roll: deque[np.ndarray] = deque(
            maxlen=max(1, int(pre_roll_ms / DEFAULT_BLOCK_MS))
        )
        self._collected: list[np.ndarray] = []
        self._in_speech = False
        self._speech_run = 0
        self._silence_run_ms = 0
        self._audio_ms = 0
        self._cooldown_until = 0.0

    # ------------------------------------------------------------------
    # model
    # ------------------------------------------------------------------
    @property
    def model_path(self) -> str:
        return self.model

    def _load_model(self):
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                cache_dir = self.cache_dir or Path.home() / ".cache" / "jarvis" / "whisper"
                cache_dir.mkdir(parents=True, exist_ok=True)
                try:
                    self._model = WhisperModel(
                        self.model,
                        device=self.device,
                        compute_type=self.compute_type,
                        download_root=str(cache_dir),
                    )
                except Exception as exc:  # noqa: BLE001 - cuBLAS may be missing
                    if self.device == "cpu":
                        raise
                    log.warning(
                        "wake Whisper на %s не загрузился (%s); перехожу на CPU",
                        self.device,
                        exc,
                    )
                    self.device = "cpu"
                    self._model = WhisperModel(
                        self.model,
                        device="cpu",
                        compute_type=self.compute_type,
                        download_root=str(cache_dir),
                    )
        return self._model

    def _transcribe(self, audio: np.ndarray) -> str:
        if self._transcriber is not None:
            return self._transcriber(audio)
        samples = np.asarray(audio)
        if np.issubdtype(samples.dtype, np.integer):
            samples = samples.astype(np.float32) / 32768.0
        else:
            samples = samples.astype(np.float32, copy=False)
        samples = samples.reshape(-1)
        model = self._load_model()
        segments, _info = model.transcribe(
            samples,
            language=self.language,
            beam_size=1,
            vad_filter=False,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()

    # ------------------------------------------------------------------
    # segmentation
    # ------------------------------------------------------------------
    def _learn_block(self, frame: np.ndarray) -> None:
        if self._block_known:
            return
        samples = int(np.asarray(frame).reshape(-1).size)
        if samples <= 0:
            return
        block_ms = max(10, int(round(samples * 1000 / self.sample_rate)))
        self._block_ms = block_ms
        self._pre_roll = deque(maxlen=max(1, int(self.pre_roll_ms / block_ms)))
        self._block_known = True

    def _reset_segment(self) -> None:
        self._collected.clear()
        self._pre_roll.clear()
        self._in_speech = False
        self._speech_run = 0
        self._silence_run_ms = 0
        self._audio_ms = 0

    def _finish_segment(self) -> bool:
        audio = (
            np.concatenate(self._collected)
            if self._collected
            else np.zeros(0, dtype=np.int16)
        )
        self._reset_segment()
        self.last_text = ""
        self.last_audio = np.zeros(0, dtype=np.int16)
        if audio.size == 0:
            return False
        started = time.monotonic()
        try:
            text = self._transcribe(audio)
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            log.warning("wake transcription failed: %s", exc)
            return False
        if text:
            log.debug(
                "wake segment %.2fs -> %r (%.2fs)",
                audio.size / self.sample_rate,
                text,
                time.monotonic() - started,
            )
        if text and contains_wake_word(text, self.phrases):
            self.last_text = text
            self.last_audio = np.asarray(audio, dtype=np.int16)
            self.last_score = 1.0
            self._cooldown_until = time.monotonic() + self.debounce
            return True
        return False

    def process(self, frame: np.ndarray) -> bool:
        frame = np.asarray(frame)
        if frame.size == 0:
            return False
        self._learn_block(frame)
        now = time.monotonic()
        self.last_score = 0.0

        is_speech = bool(self._vad.is_speech(frame))

        if now < self._cooldown_until:
            self._pre_roll.append(frame)
            return False

        if not self._in_speech:
            self._pre_roll.append(frame)
            self._speech_run = self._speech_run + 1 if is_speech else 0
            if self._speech_run * self._block_ms >= self.min_speech_ms:
                self._in_speech = True
                self._collected = list(self._pre_roll)
                self._silence_run_ms = 0
                self._audio_ms = len(self._collected) * self._block_ms
            return False

        self._collected.append(frame)
        self._audio_ms += self._block_ms
        self._silence_run_ms = 0 if is_speech else self._silence_run_ms + self._block_ms
        if (
            self._silence_run_ms >= self.silence_ms
            or self._audio_ms >= int(self.max_segment_s * 1000)
        ):
            return self._finish_segment()
        return False

    def reset(self) -> None:
        self._reset_segment()
        self._cooldown_until = 0.0
        self.last_text = ""
        self.last_audio = np.zeros(0, dtype=np.int16)
        self.last_score = 0.0
