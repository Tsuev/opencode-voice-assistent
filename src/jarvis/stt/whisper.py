"""Local speech-to-text via faster-whisper (CTranslate2)."""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

import numpy as np

from ..config import SttConfig

log = logging.getLogger(__name__)


class WhisperSTT:
    name = "faster-whisper"

    def __init__(self, config: SttConfig, *, cache_dir: Path, language: str = "ru") -> None:
        self.config = config
        self.cache_dir = cache_dir
        self.language = language if language in {"ru", "en"} else "ru"
        self._model = None
        self._lock = threading.Lock()

    def _model_options(self) -> dict[str, object]:
        options: dict[str, object] = {
            "device": self.config.device,
            "compute_type": self.config.compute_type,
            "download_root": str(self.cache_dir),
        }
        if self.config.cpu_threads:
            options["cpu_threads"] = self.config.cpu_threads
        return options

    def _load(self):
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                self.cache_dir.mkdir(parents=True, exist_ok=True)
                try:
                    self._model = WhisperModel(self.config.model, **self._model_options())
                except Exception as exc:  # noqa: BLE001 - cuBLAS may be missing
                    if self.config.device == "cpu":
                        raise
                    log.warning(
                        "faster-whisper на %s не загрузился (%s); перехожу на CPU",
                        self.config.device,
                        exc,
                    )
                    self.config.device = "cpu"
                    self._model = WhisperModel(self.config.model, **self._model_options())
        return self._model

    def warmup(self) -> None:
        """Load the model at startup instead of during the first command."""
        self._load()

    def _transcribe_sync(self, samples: np.ndarray, sample_rate: int) -> str:
        audio = np.asarray(samples)
        if np.issubdtype(audio.dtype, np.integer):
            info = np.iinfo(audio.dtype)
            divisor = float(-info.min) if info.min < 0 else float(info.max)
            audio = audio.astype(np.float32) / divisor
        else:
            audio = audio.astype(np.float32, copy=False)
        audio = audio.reshape(-1)
        if sample_rate != 16000:
            import soxr

            audio = soxr.resample(audio, sample_rate, 16000)

        model = self._load()
        options: dict[str, object] = {
            "language": self.language,
            "beam_size": self.config.beam_size,
            "vad_filter": self.config.vad_filter,
            "condition_on_previous_text": False,
            "no_speech_threshold": 0.6,
        }
        if self.config.hotwords:
            options["hotwords"] = self.config.hotwords
        if self.config.initial_prompt:
            options["initial_prompt"] = self.config.initial_prompt
        try:
            segments, _info = model.transcribe(audio, **options)
        except TypeError:
            # Older faster-whisper releases do not accept all hint options.
            options.pop("hotwords", None)
            options.pop("initial_prompt", None)
            segments, _info = model.transcribe(audio, **options)
        return " ".join(segment.text.strip() for segment in segments).strip()

    async def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> str:
        return await asyncio.to_thread(self._transcribe_sync, samples, sample_rate)
