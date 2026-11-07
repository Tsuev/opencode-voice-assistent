"""Typed configuration for Jarvis.

The config is a plain TOML file so that it stays readable and diff-friendly.
Every section maps to a pydantic model, which gives us validation and free
JSON serialisation for the future GUI (``GET /api/state``).
"""

from __future__ import annotations

import contextlib
import os
import secrets
import tempfile
import tomllib
from pathlib import Path
from typing import Literal

import tomli_w
from pydantic import BaseModel, ConfigDict, Field

from .paths import AppPaths, get_paths

WakeEngine = Literal["openwakeword", "porcupine", "vad-whisper"]
TtsEngine = Literal["piper", "kokoro", "say", "omnivoice_cpp", "moss", "omnivoice"]


class _Section(BaseModel):
    model_config = ConfigDict(extra="ignore")


class WakeConfig(_Section):
    # ``vad-whisper`` is the default because it understands the Russian
    # "Джарвис" without an account or a custom model.  English users can
    # switch to openWakeWord/Porcupine from the config or the installer.
    engine: WakeEngine = "vad-whisper"
    phrase: str = "джарвис"
    threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    model_path: str | None = None
    fallback_ru: bool = True
    debounce: float = Field(default=1.2, ge=0.0)
    access_key: str | None = None

    # --- VAD + Whisper wake detector ---------------------------------
    language: Literal["ru", "en"] = "ru"
    stt_model: str = "tiny"
    stt_device: str = "cpu"
    stt_compute_type: str = "int8"
    pre_roll_ms: int = Field(default=400, ge=0, le=3000)
    silence_ms: int = Field(default=600, ge=100, le=3000)
    min_speech_ms: int = Field(default=160, ge=40, le=2000)
    max_segment_s: float = Field(default=8.0, ge=1.0, le=30.0)

    # --- Porcupine custom Russian keyword ----------------------------
    keyword_path: str | None = None
    sensitivity: float = Field(default=0.6, ge=0.0, le=1.0)


class SttConfig(_Section):
    engine: Literal["faster-whisper", "mlx-whisper"] = "faster-whisper"
    model: str = "small"
    device: str = "cpu"
    compute_type: str = "int8"
    beam_size: int = 1
    vad_filter: bool = True


class VoiceConfig(_Section):
    engine: TtsEngine = "piper"
    voice: str = "ru_RU-ruslan-medium"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    model_path: str | None = None


class OmniVoiceCppConfig(_Section):
    """Settings for the lightweight OmniVoice.cpp sidecar (GGUF).

    The sidecar is an OpenAI-compatible HTTP server.  Jarvis starts it on
    demand and keeps it warm; the cloned voice is registered on every start
    from the local voice assets.
    """

    binary: str | None = None
    model: str | None = None
    codec: str | None = None
    host: str = "127.0.0.1"
    port: int = Field(default=47810, ge=1024, le=65535)
    device: str = "auto"  # auto | CPU | CUDA0 | Vulkan0 | MTL0
    auto_start: bool = True
    startup_timeout: float = Field(default=120.0, ge=5.0)
    request_timeout: float = Field(default=180.0, ge=5.0)
    log_file: str | None = None
    fallback: bool = True
    warmup_text: str = "Готов."


class TtsConfig(_Section):
    ru: VoiceConfig = Field(
        default_factory=lambda: VoiceConfig(engine="piper", voice="ru_RU-ruslan-medium")
    )
    en: VoiceConfig = Field(default_factory=lambda: VoiceConfig(engine="kokoro", voice="bm_george"))
    autoplay: bool = True
    omnivoice_cpp: OmniVoiceCppConfig = Field(default_factory=OmniVoiceCppConfig)


class AudioConfig(_Section):
    input_device: str | int | None = None
    output_device: str | int | None = None
    sample_rate: int = 16000
    block_ms: int = Field(default=80, ge=20, le=500)


class VadConfig(_Section):
    mode: int = Field(default=2, ge=0, le=3)
    pre_roll_ms: int = Field(default=400, ge=0, le=2000)
    silence_ms: int = Field(default=800, ge=200, le=5000)
    min_speech_ms: int = Field(default=160, ge=80, le=2000)
    max_command_s: float = Field(default=15.0, ge=1.0, le=120.0)
    wake_grace_ms: int = Field(default=1500, ge=0, le=5000)


class OpencodeConfig(_Section):
    mode: Literal["spawn", "attach"] = "spawn"
    binary: str = "opencode"
    host: str = "127.0.0.1"
    port: int | None = None
    directory: str | None = None
    agent: str = "jarvis"
    model: str = ""
    auto_approve: bool = False
    startup_timeout: float = 20.0
    turn_timeout: float = 300.0


class SpeechConfig(_Section):
    max_chars: int = Field(default=400, ge=40)
    max_sentence_chars: int = Field(default=180, ge=40)
    conversation_timeout: float = Field(default=12.0, ge=0.0)


class SafetyConfig(_Section):
    permission_timeout: float = Field(default=15.0, ge=1.0)


class ApiConfig(_Section):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 47800
    token: str | None = None
    cors_origins: list[str] = Field(default_factory=list)


class GuiConfig(_Section):
    """Reserved for the future GUI.

    Nothing here affects the CLI today; the GUI reads the same REST + SSE
    contract exposed by :mod:`jarvis.api`.
    """

    enabled: bool = False
    theme: str = "jarvis-dark"
    window: dict[str, object] = Field(default_factory=dict)


class JarvisConfig(_Section):
    language: Literal["ru", "en"] = "ru"
    wake: WakeConfig = Field(default_factory=WakeConfig)
    stt: SttConfig = Field(default_factory=SttConfig)
    tts: TtsConfig = Field(default_factory=TtsConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    vad: VadConfig = Field(default_factory=VadConfig)
    opencode: OpencodeConfig = Field(default_factory=OpencodeConfig)
    speech: SpeechConfig = Field(default_factory=SpeechConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    gui: GuiConfig = Field(default_factory=GuiConfig)
    log_level: str = "info"


def load_config(path: Path | None = None, paths: AppPaths | None = None) -> JarvisConfig:
    """Load config from TOML, migrating the old ``[stt] language`` field.

    Before the language was fixed at the top level, ``[stt] language`` could be
    ``"auto"``.  That sent ``language=None`` to Whisper and made short phrases
    (for example «Ассаламу Алайкум») decode as Arabic.  The migration is
    intentionally silent in the file: the next ``save_config`` writes the new
    top-level key.
    """
    paths = paths or get_paths()
    config_path = path or paths.config_file
    if not config_path.exists():
        return JarvisConfig()
    with config_path.open("rb") as handle:
        data = tomllib.load(handle)

    stt_section = data.get("stt")
    if isinstance(stt_section, dict):
        legacy = stt_section.pop("language", None)
        if "language" not in data and legacy in {"ru", "en"}:
            data["language"] = legacy

    if data.get("language") not in {"ru", "en"}:
        data["language"] = "ru"
    return JarvisConfig.model_validate(data)


def save_config(
    config: JarvisConfig, path: Path | None = None, paths: AppPaths | None = None
) -> Path:
    """Atomically persist config as TOML."""
    paths = paths or get_paths()
    config_path = path or paths.config_file
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = config.model_dump(mode="json", exclude_none=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".config-", suffix=".toml", dir=config_path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            tomli_w.dump(payload, handle)
        os.replace(tmp_name, config_path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return config_path


def ensure_api_token(paths: AppPaths) -> str:
    """Create (or reuse) the bearer token for the local GUI/plugin API."""
    paths.ensure()
    token_file = paths.token_file
    if token_file.exists():
        token = token_file.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    token_file.write_text(token + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        token_file.chmod(0o600)
    return token


def init_config(paths: AppPaths | None = None, *, force: bool = False) -> tuple[JarvisConfig, Path]:
    """Create the default config + API token. Idempotent unless ``force``."""
    paths = (paths or get_paths()).ensure()
    config_path = paths.config_file
    if config_path.exists() and not force:
        config = load_config(config_path, paths)
    else:
        config = JarvisConfig()
        save_config(config, config_path, paths)
    ensure_api_token(paths)
    return config, config_path
