"""Selects the right local voice for each reply."""

from __future__ import annotations

import asyncio

import numpy as np
from rich.console import Console

from ..config import TtsConfig, VoiceConfig
from ..paths import AppPaths
from .base import Audio
from .kokoro import KokoroTTS
from .piper import PiperTTS
from .say import SayTTS


class TTSRouter:
    """Caches providers so models are loaded once per process."""

    def __init__(
        self,
        config: TtsConfig,
        paths: AppPaths,
        *,
        console: Console | None = None,
        output_device: str | int | None = None,
    ) -> None:
        self.config = config
        self.paths = paths
        self.console = console
        self.output_device = output_device
        self._cache: dict[tuple[str, str, str], object] = {}

    def voice_config(self, language: str | None) -> VoiceConfig:
        if language == "ru":
            return self.config.ru
        return self.config.en

    def provider(self, voice: VoiceConfig):
        key = (voice.engine, voice.voice, str(voice.model_path or ""))
        if key in self._cache:
            return self._cache[key]
        if voice.engine == "piper":
            provider = PiperTTS(
                voice.voice,
                self.paths.voices_dir,
                model_path=voice.model_path,
                console=self.console,
            )
        elif voice.engine == "kokoro":
            provider = KokoroTTS(
                voice.voice,
                self.paths.models_dir,
                model_path=voice.model_path,
                console=self.console,
            )
        elif voice.engine == "say":
            provider = SayTTS(voice.voice)
        else:  # pragma: no cover - validated by pydantic
            raise ValueError(f"Неизвестный TTS-движок: {voice.engine}")
        self._cache[key] = provider
        return provider

    async def synthesize(
        self,
        text: str,
        *,
        language: str | None = None,
        speed: float | None = None,
    ) -> Audio:
        text = text.strip()
        if not text:
            return Audio(np.zeros(0, dtype=np.float32), 22050)
        resolved = language if language in {"ru", "en"} else "ru"
        voice = self.voice_config(resolved)
        provider = self.provider(voice)
        return await provider.synthesize(  # type: ignore[attr-defined]
            text,
            voice=voice.voice,
            speed=voice.speed if speed is None else speed,
        )

    async def speak(
        self,
        text: str,
        *,
        language: str | None = None,
        speed: float | None = None,
        play: bool = True,
    ) -> Audio:
        audio = await self.synthesize(text, language=language, speed=speed)
        if play and not audio.empty:
            from ..audio.output import play_audio

            await asyncio.to_thread(play_audio, audio, self.output_device)
        return audio

    def status(self) -> list[dict[str, object]]:
        """Lightweight provider availability, used by ``jarvis doctor``."""
        piper_model = self.paths.voices_dir / f"{self.config.ru.voice}.onnx"
        kokoro_model = self.paths.models_dir / "kokoro-v1.0.onnx"
        kokoro_voices = self.paths.models_dir / "voices-v1.0.bin"
        return [
            {
                "engine": "piper",
                "voice": self.config.ru.voice,
                "ready": piper_model.exists(),
                "path": str(piper_model),
            },
            {
                "engine": "kokoro",
                "voice": self.config.en.voice,
                "ready": kokoro_model.exists() and kokoro_voices.exists(),
                "path": str(kokoro_model),
            },
            {
                "engine": "say",
                "voice": "system",
                "ready": SayTTS.available(),
                "path": "system",
            },
        ]
