from __future__ import annotations

import io
import json
from pathlib import Path

import httpx
import numpy as np
import pytest
import soundfile as sf

from jarvis.config import OmniVoiceCppConfig
from jarvis.paths import AppPaths
from jarvis.tts.omnivoice_cpp import OmniVoiceCppError, OmniVoiceCppTTS
from jarvis.tts.voice_import import import_from_file


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "logs",
    )


def _wav_bytes(seconds: float = 0.1, sample_rate: int = 24000) -> bytes:
    buffer = io.BytesIO()
    samples = np.zeros(int(seconds * sample_rate), dtype=np.float32)
    sf.write(buffer, samples, sample_rate, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


def _prepare_voice(tmp_path: Path, paths: AppPaths) -> None:
    source = tmp_path / "ref.wav"
    sf.write(source, np.zeros(8000, dtype=np.float32), 16000, subtype="PCM_16")
    import_from_file("JARVIS", source, "Тестовый референс.", paths, target="jarvis")


@pytest.mark.asyncio
async def test_omnivoice_cpp_registers_voice_and_synthesizes(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _prepare_voice(tmp_path, paths)
    requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v1/audio/voices" and request.method == "GET":
            return httpx.Response(200, json={"voices": []})
        if request.url.path == "/v1/audio/voices" and request.method == "POST":
            requests.append(("register", json.loads(request.content)))
            return httpx.Response(200, json={"name": "jarvis", "status": "registered"})
        if request.url.path == "/v1/audio/speech":
            requests.append(("speech", json.loads(request.content)))
            return httpx.Response(200, content=_wav_bytes(), headers={"content-type": "audio/wav"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:47810")
    provider = OmniVoiceCppTTS(
        "jarvis",
        paths,
        OmniVoiceCppConfig(port=47810),
        client=client,
    )

    audio = await provider.synthesize("Привет", language="ru")

    assert audio.sample_rate == 24000
    assert audio.samples.size > 0
    kinds = [kind for kind, _ in requests]
    assert "register" in kinds
    speech = next(payload for kind, payload in requests if kind == "speech")
    assert speech["voice"] == "jarvis"
    assert speech["language"] == "Russian"
    assert speech["response_format"] == "wav"
    await provider.close()


@pytest.mark.asyncio
async def test_omnivoice_cpp_reports_missing_voice(tmp_path: Path) -> None:
    paths = _paths(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"voices": []})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:47810"
    )
    provider = OmniVoiceCppTTS("jarvis", paths, OmniVoiceCppConfig(), client=client)

    with pytest.raises(OmniVoiceCppError):
        await provider.synthesize("Привет", language="ru")
    await provider.close()
