"""Kokoro-82M TTS adapter (local, best free British male voice)."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from pathlib import Path

import numpy as np
from rich.console import Console

from .base import Audio
from .downloads import ensure_kokoro


def kokoro_lang(voice: str) -> str:
    """Map a Kokoro voice id to the language tag expected by ``create``."""
    if voice.startswith(("bf_", "bm_")):
        return "en-gb"
    if voice.startswith(("af_", "am_")):
        return "en-us"
    if voice.startswith(("ef_", "em_")):
        return "es"
    if voice.startswith("ff_"):
        return "fr"
    if voice.startswith(("hf_", "hm_")):
        return "hi"
    if voice.startswith(("if_", "im_")):
        return "it"
    if voice.startswith(("jf_", "jm_")):
        return "ja"
    if voice.startswith(("pf_", "pm_")):
        return "pt-br"
    if voice.startswith(("zf_", "zm_")):
        return "zh"
    return "en-us"


class KokoroTTS:
    name = "kokoro"

    def __init__(
        self,
        voice: str,
        models_dir: Path,
        *,
        model_path: str | Path | None = None,
        console: Console | None = None,
    ) -> None:
        self.voice = voice
        self.models_dir = Path(models_dir)
        self._model_override = Path(model_path).expanduser() if model_path else None
        self._console = console
        self._kokoro = None
        self._lock = threading.Lock()

    def paths(self) -> tuple[Path, Path]:
        if self._model_override is not None:
            voices = self._model_override.with_name("voices-v1.0.bin")
            return self._model_override, voices
        return ensure_kokoro(self.models_dir, console=self._console)

    def _load(self):
        if self._kokoro is not None:
            return self._kokoro
        with self._lock:
            if self._kokoro is None:
                from kokoro_onnx import Kokoro

                model_path, voices_path = self.paths()
                self._kokoro = Kokoro(str(model_path), str(voices_path))
        return self._kokoro

    def _synthesize_sync(self, text: str, voice: str, speed: float) -> Audio:
        kokoro = self._load()
        samples, sample_rate = kokoro.create(
            text,
            voice=voice,
            speed=speed,
            lang=kokoro_lang(voice),
        )
        return Audio(np.asarray(samples, dtype=np.float32), int(sample_rate))

    async def synthesize(
        self,
        text: str,
        voice: str | None = None,
        speed: float = 1.0,
        language: str | None = None,
    ) -> Audio:
        return await asyncio.to_thread(self._synthesize_sync, text, voice or self.voice, speed)

    async def warmup(self) -> None:
        """Load the ONNX model and warm its allocator."""
        await asyncio.to_thread(self._load)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(
                self._synthesize_sync, "Готов.", self.voice, 1.0
            )
