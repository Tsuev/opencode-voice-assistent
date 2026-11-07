"""Selects the right local voice for each reply."""

from __future__ import annotations

import asyncio
import logging

import numpy as np
from rich.console import Console

from ..config import TtsConfig, VoiceConfig
from ..paths import AppPaths
from .base import Audio
from .kokoro import KokoroTTS
from .omnivoice_cpp import OmniVoiceCppError, OmniVoiceCppTTS
from .piper import PiperTTS
from .say import SayTTS

log = logging.getLogger(__name__)


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
        self._cache: dict[tuple[str, str, str, str], object] = {}

    def voice_config(self, language: str | None) -> VoiceConfig:
        if language == "ru":
            return self.config.ru
        return self.config.en

    def _cache_extra(self, voice: VoiceConfig) -> str:
        if voice.engine != "omnivoice_cpp":
            return ""
        settings = self.config.omnivoice_cpp
        return f"{settings.host}:{settings.port}:{settings.binary or ''}:{settings.model or ''}"

    def provider(self, voice: VoiceConfig):
        key = (voice.engine, voice.voice, str(voice.model_path or ""), self._cache_extra(voice))
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
        elif voice.engine == "omnivoice_cpp":
            provider = OmniVoiceCppTTS(
                voice.voice,
                self.paths,
                self.config.omnivoice_cpp,
                console=self.console,
            )
        else:  # pragma: no cover - validated by pydantic
            raise ValueError(
                f"TTS-движок «{voice.engine}» пока не подключён. "
                "Доступны: piper, kokoro, say, omnivoice_cpp."
            )
        self._cache[key] = provider
        return provider

    def fallback_voice(self, language: str) -> VoiceConfig:
        configured = self.voice_config(language)
        if configured.engine != "omnivoice_cpp":
            return configured
        if language == "ru":
            return VoiceConfig(engine="piper", voice="ru_RU-ruslan-medium")
        return VoiceConfig(engine="kokoro", voice="bm_george")

    async def _synthesize_with(
        self,
        provider,
        text: str,
        voice: VoiceConfig,
        language: str,
        speed: float,
    ) -> Audio:
        return await provider.synthesize(
            text,
            voice=voice.voice,
            speed=speed,
            language=language,
        )

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
        resolved_speed = voice.speed if speed is None else speed
        try:
            return await self._synthesize_with(
                provider, text, voice, resolved, resolved_speed
            )
        except OmniVoiceCppError as exc:
            if voice.engine != "omnivoice_cpp" or not self.config.omnivoice_cpp.fallback:
                raise
            fallback = self.fallback_voice(resolved)
            log.warning(
                "OmniVoice.cpp недоступен (%s); перехожу на %s/%s",
                exc,
                fallback.engine,
                fallback.voice,
            )
            return await self._synthesize_with(
                self.provider(fallback), text, fallback, resolved, resolved_speed
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

    async def warmup(self, language: str | None = None) -> None:
        languages = [language] if language in {"ru", "en"} else ["ru", "en"]
        for lang in languages:
            voice = self.voice_config(lang)
            provider = self.provider(voice)
            warmup = getattr(provider, "warmup", None)
            if warmup is not None:
                await warmup()

    async def close(self) -> None:
        seen: set[int] = set()
        for provider in list(self._cache.values()):
            if id(provider) in seen:
                continue
            seen.add(id(provider))
            close = getattr(provider, "close", None)
            if close is not None:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
        self._cache.clear()

    def status(self) -> list[dict[str, object]]:
        """Lightweight provider availability, used by ``jarvis doctor``."""
        piper_voice = (
            self.config.ru.voice if self.config.ru.engine == "piper" else "ru_RU-ruslan-medium"
        )
        kokoro_voice = (
            self.config.en.voice if self.config.en.engine == "kokoro" else "bm_george"
        )
        piper_model = self.paths.voices_dir / f"{piper_voice}.onnx"
        kokoro_model = self.paths.models_dir / "kokoro-v1.0.onnx"
        kokoro_voices = self.paths.models_dir / "voices-v1.0.bin"
        omni_voice = "jarvis"
        if self.config.ru.engine == "omnivoice_cpp":
            omni_voice = self.config.ru.voice
        elif self.config.en.engine == "omnivoice_cpp":
            omni_voice = self.config.en.voice
        omni_ready, omni_reason = OmniVoiceCppTTS.available(
            self.paths, self.config.omnivoice_cpp, omni_voice
        )
        return [
            {
                "engine": "piper",
                "voice": piper_voice,
                "ready": piper_model.exists(),
                "path": str(piper_model),
            },
            {
                "engine": "kokoro",
                "voice": kokoro_voice,
                "ready": kokoro_model.exists() and kokoro_voices.exists(),
                "path": str(kokoro_model),
            },
            {
                "engine": "omnivoice_cpp",
                "voice": omni_voice,
                "ready": omni_ready,
                "path": str(self.config.omnivoice_cpp.binary or self.paths.bin_dir),
                "detail": omni_reason,
            },
            {
                "engine": "say",
                "voice": "system",
                "ready": SayTTS.available(),
                "path": "system",
            },
        ]
