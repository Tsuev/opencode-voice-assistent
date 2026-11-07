"""Piper TTS adapter (local, fast, supports Russian)."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from pathlib import Path

import numpy as np
from rich.console import Console

from .base import Audio
from .downloads import ensure_piper_voice


class PiperTTS:
    name = "piper"

    def __init__(
        self,
        voice: str,
        voices_dir: Path,
        *,
        model_path: str | Path | None = None,
        console: Console | None = None,
    ) -> None:
        self.voice = voice
        self.voices_dir = Path(voices_dir)
        self._model_override = Path(model_path).expanduser() if model_path else None
        self._console = console
        self._loaded = None
        self._lock = threading.Lock()

    @property
    def model_path(self) -> Path:
        if self._model_override is not None:
            return self._model_override
        return self.voices_dir / f"{self.voice}.onnx"

    def ensure_model(self) -> Path:
        if self._model_override is not None:
            if not self._model_override.exists():
                raise FileNotFoundError(f"Модель Piper не найдена: {self._model_override}")
            return self._model_override
        return ensure_piper_voice(self.voice, self.voices_dir, console=self._console)

    def _load(self):
        if self._loaded is not None:
            return self._loaded
        with self._lock:
            if self._loaded is None:
                from piper import PiperVoice

                model_path = self.ensure_model()
                self._loaded = PiperVoice.load(model_path)
        return self._loaded

    def _synthesize_sync(self, text: str, speed: float) -> Audio:
        from piper import SynthesisConfig

        voice = self._load()
        config = SynthesisConfig(length_scale=1.0 / max(speed, 0.1))
        chunks = list(voice.synthesize(text, config))
        if not chunks:
            return Audio(np.zeros(0, dtype=np.float32), 22050)
        samples = np.concatenate([chunk.audio_float_array for chunk in chunks])
        return Audio(samples.astype(np.float32, copy=False), chunks[0].sample_rate)

    async def synthesize(
        self,
        text: str,
        voice: str | None = None,
        speed: float = 1.0,
        language: str | None = None,
    ) -> Audio:
        return await asyncio.to_thread(self._synthesize_sync, text, speed)

    async def warmup(self) -> None:
        """Load the ONNX model and warm its allocator."""
        await asyncio.to_thread(self._load)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self._synthesize_sync, "Готов.", 1.0)
