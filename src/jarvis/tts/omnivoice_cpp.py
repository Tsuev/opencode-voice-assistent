"""OmniVoice.cpp sidecar TTS provider.

The C++/GGML port of OmniVoice exposes an OpenAI-compatible HTTP server
(``tts-server``) with a cloned-voice registry.  It keeps the model resident in
one process, uses ~1 GB of memory for the Q8 GGUF, and runs on CPU, CUDA,
Vulkan and Metal.  Jarvis starts it on demand and talks to it over loopback.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from ..config import OmniVoiceCppConfig
from ..paths import AppPaths
from .base import Audio
from .voice_import import VoiceAsset, VoiceImportError, load_asset

log = logging.getLogger(__name__)

_LANGUAGE_NAMES = {"ru": "Russian", "en": "English"}
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class OmniVoiceCppError(RuntimeError):
    pass


def _executable_name() -> str:
    return "tts-server.exe" if sys.platform == "win32" else "tts-server"


def _find_first(directory: Path, pattern: str) -> Path | None:
    if not directory.exists():
        return None
    matches = sorted(directory.glob(pattern))
    return matches[0] if matches else None


class OmniVoiceCppTTS:
    name = "omnivoice_cpp"

    def __init__(
        self,
        voice: str,
        paths: AppPaths,
        config: OmniVoiceCppConfig,
        *,
        console=None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.voice = voice
        self.paths = paths
        self.config = config
        self.console = console
        self._client = client
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = asyncio.Lock()
        self._registered = False
        self._base_url = f"http://{config.host}:{config.port}"
        self._asset: VoiceAsset | None = None

    # ------------------------------------------------------------------
    # paths
    # ------------------------------------------------------------------
    @property
    def binary_path(self) -> Path:
        if self.config.binary:
            return Path(self.config.binary).expanduser()
        return self.paths.bin_dir / _executable_name()

    @property
    def models_dir(self) -> Path:
        return self.paths.models_dir / "omnivoice-cpp"

    @property
    def model_path(self) -> Path:
        if self.config.model:
            return Path(self.config.model).expanduser()
        found = _find_first(self.models_dir, "omnivoice-base-*.gguf")
        return found or (self.models_dir / "omnivoice-base-Q8_0.gguf")

    @property
    def codec_path(self) -> Path:
        if self.config.codec:
            return Path(self.config.codec).expanduser()
        found = _find_first(self.models_dir, "omnivoice-tokenizer-*.gguf")
        return found or (self.models_dir / "omnivoice-tokenizer-Q8_0.gguf")

    @property
    def log_path(self) -> Path:
        if self.config.log_file:
            return Path(self.config.log_file).expanduser()
        return self.paths.log_dir / "omnivoice-cpp.log"

    def asset(self) -> VoiceAsset:
        if self._asset is None:
            try:
                self._asset = load_asset(self.voice, self.paths)
            except VoiceImportError as exc:
                raise OmniVoiceCppError(str(exc)) from exc
        return self._asset

    def model_ready(self) -> tuple[bool, str]:
        binary = self.binary_path
        if not binary.exists():
            return False, f"нет tts-server: {binary}"
        if not self.model_path.exists():
            return False, f"нет модели: {self.model_path}"
        if not self.codec_path.exists():
            return False, f"нет codec: {self.codec_path}"
        return True, "готов"

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(self.config.request_timeout, connect=5.0),
            )
        return self._client

    def _start_process(self) -> subprocess.Popen[bytes]:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.log_path.open("ab")
        log.info("Запускаю OmniVoice.cpp: %s", self.binary_path)
        args = [
            str(self.binary_path),
            "--model",
            str(self.model_path),
            "--codec",
            str(self.codec_path),
            "--host",
            self.config.host,
            "--port",
            str(self.config.port),
        ]
        if self.config.device and self.config.device.lower() != "auto":
            args.extend(["--device", self.config.device])
        try:
            process = subprocess.Popen(
                args,
                cwd=str(self.model_path.parent),
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                creationflags=_CREATE_NO_WINDOW,
            )
        except OSError as exc:
            handle.close()
            raise OmniVoiceCppError(f"Не удалось запустить {self.binary_path}: {exc}") from exc
        handle.close()
        return process

    def _log_tail(self, lines: int = 20) -> str:
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])

    async def _health(self) -> bool:
        client = self._client_or_create()
        try:
            response = await client.get("/health", timeout=2.0)
        except (httpx.HTTPError, OSError):
            return False
        if response.status_code != 200:
            return False
        try:
            payload = response.json()
        except ValueError:
            return False
        return bool(payload.get("status") == "ok")

    async def _wait_for_health(self) -> None:
        deadline = time.monotonic() + self.config.startup_timeout
        while time.monotonic() < deadline:
            if await self._health():
                return
            if self._process is not None and self._process.poll() is not None:
                code = self._process.returncode
                raise OmniVoiceCppError(
                    f"tts-server завершился с кодом {code}.\n{self._log_tail()}"
                )
            await asyncio.sleep(0.5)
        raise OmniVoiceCppError(
            "tts-server не ответил за "
            f"{self.config.startup_timeout:.0f} с. Лог: {self.log_path}\n{self._log_tail()}"
        )

    async def _ensure_server(self) -> None:
        async with self._lock:
            if await self._health():
                # The server may have been restarted externally; the voice
                # registry lives in memory, so always make sure ours is there.
                await self._register_voice()
                return

            ready, reason = self.model_ready()
            if not ready:
                raise OmniVoiceCppError(
                    f"OmniVoice.cpp не готов: {reason}. "
                    "Выполните: jarvis voices install --engine omnivoice-cpp"
                )
            if not self.config.auto_start:
                raise OmniVoiceCppError(
                    f"tts-server не запущен на {self._base_url}, а auto_start=false"
                )

            self._process = await asyncio.to_thread(self._start_process)
            await self._wait_for_health()
            self._registered = False
            await self._register_voice()

    async def _register_voice(self) -> None:
        asset = self.asset()
        client = self._client_or_create()
        try:
            response = await client.get("/v1/audio/voices", timeout=10.0)
            response.raise_for_status()
            names = {item.get("name") for item in response.json().get("voices", [])}
        except (httpx.HTTPError, ValueError):
            names = set()

        if self.voice in names:
            self._registered = True
            return

        payload: dict[str, Any] = {
            "name": self.voice,
            "ref_text": asset.reference_text,
            "wav_b64": base64.b64encode(asset.reference_audio.read_bytes()).decode("ascii"),
        }
        try:
            response = await client.post("/v1/audio/voices", json=payload, timeout=120.0)
        except httpx.HTTPError as exc:
            raise OmniVoiceCppError(f"Не удалось зарегистрировать голос: {exc}") from exc
        if response.status_code >= 400:
            raise OmniVoiceCppError(
                "tts-server отклонил голос "
                f"«{self.voice}»: HTTP {response.status_code} {response.text[:300]}"
            )
        self._registered = True

    async def close(self) -> None:
        process = self._process
        self._process = None
        self._registered = False
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, 5.0)
            except subprocess.TimeoutExpired:
                process.kill()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # synthesis
    # ------------------------------------------------------------------
    def _decode_wav(self, content: bytes) -> Audio:
        try:
            import soundfile as sf
        except ImportError as exc:  # pragma: no cover
            raise OmniVoiceCppError("soundfile не установлен") from exc
        data, sample_rate = sf.read(io.BytesIO(content), dtype="float32", always_2d=True)
        if data.size == 0:
            raise OmniVoiceCppError("tts-server вернул пустое аудио")
        if data.shape[1] > 1:
            samples = np.asarray(data, dtype=np.float32).mean(axis=1)
        else:
            samples = np.asarray(data[:, 0], dtype=np.float32)
        return Audio(samples, int(sample_rate))

    async def warmup(self) -> None:
        await self._ensure_server()

    async def synthesize(
        self,
        text: str,
        voice: str | None = None,
        speed: float = 1.0,
        language: str | None = None,
    ) -> Audio:
        text = text.strip()
        if not text:
            return Audio(np.zeros(0, dtype=np.float32), 24000)
        await self._ensure_server()
        payload = {
            "input": text,
            "voice": self.voice,
            "response_format": "wav",
        }
        if language in _LANGUAGE_NAMES:
            payload["language"] = _LANGUAGE_NAMES[language]
        client = self._client_or_create()
        try:
            response = await client.post(
                "/v1/audio/speech",
                json=payload,
                timeout=self.config.request_timeout,
            )
        except httpx.HTTPError as exc:
            raise OmniVoiceCppError(f"OmniVoice.cpp недоступен: {exc}") from exc
        if response.status_code >= 400:
            raise OmniVoiceCppError(
                f"OmniVoice.cpp: HTTP {response.status_code} {response.text[:400]}"
            )
        return self._decode_wav(response.content)

    @classmethod
    def available(
        cls,
        paths: AppPaths,
        config: OmniVoiceCppConfig,
        voice: str = "jarvis",
    ) -> tuple[bool, str]:
        probe = cls(voice, paths, config)
        ready, reason = probe.model_ready()
        if not ready:
            return False, reason
        try:
            probe.asset()
        except OmniVoiceCppError as exc:
            return False, str(exc)
        return True, "готов"
