from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from jarvis.config import JarvisConfig
from jarvis.paths import AppPaths
from jarvis.tts.base import Audio
from jarvis.tts.omnivoice_cpp import OmniVoiceCppError
from jarvis.tts.router import TTSRouter


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "logs",
    )


class _Boom:
    async def synthesize(self, text, voice=None, speed=1.0, language=None):
        raise OmniVoiceCppError("server is down")


class _Ok:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def synthesize(self, text, voice=None, speed=1.0, language=None):
        self.calls.append((voice or "", language))
        return Audio(np.ones(100, dtype=np.float32), 24000)


@pytest.mark.asyncio
async def test_router_falls_back_to_piper_when_omnivoice_fails(tmp_path: Path) -> None:
    config = JarvisConfig()
    config.tts.ru.engine = "omnivoice_cpp"
    config.tts.ru.voice = "jarvis"
    config.tts.omnivoice_cpp.fallback = True
    router = TTSRouter(config.tts, _paths(tmp_path))
    ok = _Ok()

    def provider(voice):
        return _Boom() if voice.engine == "omnivoice_cpp" else ok

    router.provider = provider  # type: ignore[method-assign]

    audio = await router.synthesize("Привет", language="ru")

    assert audio.sample_rate == 24000
    assert ok.calls == [("ru_RU-ruslan-medium", "ru")]


@pytest.mark.asyncio
async def test_router_does_not_fall_back_when_disabled(tmp_path: Path) -> None:
    config = JarvisConfig()
    config.tts.ru.engine = "omnivoice_cpp"
    config.tts.ru.voice = "jarvis"
    config.tts.omnivoice_cpp.fallback = False
    router = TTSRouter(config.tts, _paths(tmp_path))
    router.provider = lambda voice: _Boom()  # type: ignore[method-assign]

    with pytest.raises(OmniVoiceCppError):
        await router.synthesize("Привет", language="ru")
