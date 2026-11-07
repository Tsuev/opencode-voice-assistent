"""Local speech-to-text via faster-whisper (CTranslate2)."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import numpy as np

from ..config import SttConfig


class WhisperSTT:
    name = "faster-whisper"

    def __init__(self, config: SttConfig, *, cache_dir: Path, language: str = "ru") -> None:
        self.config = config
        self.cache_dir = cache_dir
        self.language = language if language in {"ru", "en"} else "ru"
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                self.cache_dir.mkdir(parents=True, exist_ok=True)
                self._model = WhisperModel(
                    self.config.model,
                    device=self.config.device,
                    compute_type=self.config.compute_type,
                    download_root=str(self.cache_dir),
                )
        return self._model

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
        segments, _info = model.transcribe(
            audio,
            language=self.language,
            beam_size=self.config.beam_size,
            vad_filter=self.config.vad_filter,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()

    async def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> str:
        return await asyncio.to_thread(self._transcribe_sync, samples, sample_rate)
