from __future__ import annotations

from pathlib import Path

import numpy as np

from jarvis.config import SttConfig
from jarvis.stt.whisper import WhisperSTT


class FakeModel:
    def __init__(self) -> None:
        self.audio: np.ndarray | None = None
        self.kwargs: dict[str, object] = {}

    def transcribe(self, audio, **kwargs):
        self.audio = audio
        self.kwargs = kwargs
        return [], None


async def test_int16_audio_is_normalized(tmp_path: Path) -> None:
    stt = WhisperSTT(SttConfig(), cache_dir=tmp_path, language="ru")
    fake = FakeModel()
    stt._model = fake  # type: ignore[assignment]

    await stt.transcribe(np.array([32767, -32768, 0], dtype=np.int16), 16000)

    assert fake.audio is not None
    assert fake.audio.dtype == np.float32
    assert float(np.max(np.abs(fake.audio))) <= 1.0


async def test_float_audio_is_kept(tmp_path: Path) -> None:
    stt = WhisperSTT(SttConfig(), cache_dir=tmp_path, language="en")
    fake = FakeModel()
    stt._model = fake  # type: ignore[assignment]

    await stt.transcribe(np.array([0.5, -0.5], dtype=np.float32), 16000)

    assert fake.audio is not None
    assert np.max(np.abs(fake.audio)) <= 1.0
    assert fake.kwargs["language"] == "en"


async def test_language_is_always_explicit(tmp_path: Path) -> None:
    stt = WhisperSTT(SttConfig(), cache_dir=tmp_path, language="ru")
    fake = FakeModel()
    stt._model = fake  # type: ignore[assignment]

    await stt.transcribe(np.zeros(16000, dtype=np.float32), 16000)

    assert fake.kwargs["language"] == "ru"
    assert fake.kwargs["language"] is not None
