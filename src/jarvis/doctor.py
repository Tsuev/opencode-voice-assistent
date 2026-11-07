"""Environment diagnostics used by ``jarvis doctor``."""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from .config import JarvisConfig
from .opencode.agent import agent_target
from .opencode.client import list_models
from .paths import AppPaths
from .toolchain import detect_toolchain

Status = Literal["ok", "warn", "fail"]


@dataclass(slots=True)
class Check:
    name: str
    status: Status
    detail: str
    fix: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _run(cmd: list[str], timeout: float = 10.0) -> tuple[int, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode, (result.stdout or result.stderr or "").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)


def run_checks(config: JarvisConfig, paths: AppPaths) -> list[Check]:
    checks: list[Check] = []

    # Python / package manager -------------------------------------------------
    version = sys.version_info
    if (version.major, version.minor) == (3, 12):
        checks.append(Check("python", "ok", f"{sys.version.split()[0]}"))
    elif (version.major, version.minor) >= (3, 11):
        checks.append(
            Check(
                "python",
                "warn",
                f"{sys.version.split()[0]} — рекомендуется 3.12",
                "uv python pin 3.12 && uv sync",
            )
        )
    else:
        checks.append(
            Check("python", "fail", f"{sys.version.split()[0]} — нужен 3.12", "uv python pin 3.12")
        )

    uv = shutil.which("uv")
    checks.append(
        Check(
            "uv",
            "ok" if uv else "warn",
            uv or "не найден",
            None if uv else "curl -LsSf https://astral.sh/uv/install.sh | sh",
        )
    )

    # Native toolchain -------------------------------------------------------
    toolchain = detect_toolchain()
    cmake_detail = str(toolchain.cmake or "не найден")
    if toolchain.cmake_version:
        cmake_detail += f" ({toolchain.cmake_version})"
    checks.append(
        Check(
            "cmake",
            "ok" if toolchain.cmake else "warn",
            cmake_detail,
            None
            if toolchain.cmake
            else "Windows: winget install Kitware.CMake; macOS: brew install cmake",
        )
    )
    if sys.platform == "win32":
        checks.append(
            Check(
                "c++ compiler",
                "ok" if toolchain.has_compiler else "warn",
                str(toolchain.cl or toolchain.gpp or "не найден"),
                None
                if toolchain.has_compiler
                else "Visual Studio Build Tools → Desktop development with C++",
            )
        )
        checks.append(
            Check(
                "nvcc",
                "ok" if toolchain.nvcc else "warn",
                str(toolchain.nvcc or "не найден"),
                None
                if toolchain.nvcc
                else "CUDA Toolkit 12.x нужен для быстрого OmniVoice.cpp",
            )
        )
        if toolchain.ninja:
            checks.append(Check("ninja", "ok", str(toolchain.ninja)))

    # OpenCode ---------------------------------------------------------------
    opencode = shutil.which(config.opencode.binary)
    if opencode:
        code, out = _run([opencode, "--version"])
        checks.append(
            Check(
                "opencode",
                "ok" if code == 0 else "warn",
                out or opencode,
                None if code == 0 else "opencode upgrade",
            )
        )
    else:
        checks.append(
            Check(
                "opencode",
                "fail",
                f"{config.opencode.binary!r} не найден в PATH",
                "Установите OpenCode: https://opencode.ai/docs",
            )
        )

    if config.opencode.mode == "attach":
        code, out = _run([config.opencode.binary, "service", "status"])
        checks.append(
            Check(
                "opencode service",
                "ok" if code == 0 and out.startswith("http") else "warn",
                out if code == 0 else "сервис не запущен",
                "opencode service start или opencode.mode=spawn",
            )
        )
    else:
        checks.append(Check("opencode mode", "ok", "spawn — отдельный сервер на время работы"))

    models = list_models(config.opencode.binary) if opencode else []
    if config.opencode.model:
        found = config.opencode.model in models
        checks.append(
            Check(
                "model",
                "ok" if found or not models else "warn",
                config.opencode.model,
                None
                if found or not models
                else f"Модель не найдена. Доступны: {', '.join(models[:5])}…",
            )
        )
    else:
        checks.append(
            Check(
                "model",
                "warn",
                "не зафиксирована — используется модель OpenCode по умолчанию",
                "Укажите opencode.model, например local-deepseek/deepseek-chat",
            )
        )

    agent = agent_target()
    agent_exists = agent.exists()
    agent_language_ok = True
    if agent_exists:
        text = agent.read_text(encoding="utf-8", errors="ignore")
        expected = "по-русски" if config.language == "ru" else "Always answer in English"
        agent_language_ok = expected in text
    checks.append(
        Check(
            "agent",
            "ok" if agent_exists and agent_language_ok else "warn",
            str(agent)
            + (f" ({config.language})" if agent_language_ok else " — язык не совпадает с конфигом"),
            None if agent_exists and agent_language_ok else f"jarvis language {config.language}",
        )
    )

    # Config / API -----------------------------------------------------------
    checks.append(
        Check(
            "config",
            "ok" if paths.config_file.exists() else "warn",
            str(paths.config_file),
            None if paths.config_file.exists() else "jarvis init",
        )
    )
    checks.append(Check("language", "ok", f"{config.language} (stt/tts/agent)"))
    checks.append(
        Check(
            "api token",
            "ok" if config.api.token or paths.token_file.exists() else "warn",
            "настроен"
            if config.api.token or paths.token_file.exists()
            else "нет (API без авторизации)",
            None if config.api.token or paths.token_file.exists() else "jarvis init",
        )
    )

    # Audio ------------------------------------------------------------------
    try:
        import sounddevice as sd

        devices = sd.query_devices()
        inputs = [d for d in devices if d.get("max_input_channels", 0) > 0]
        outputs = [d for d in devices if d.get("max_output_channels", 0) > 0]
        default_in, default_out = sd.default.device
        in_name = devices[default_in]["name"] if 0 <= default_in < len(devices) else "нет"
        out_name = devices[default_out]["name"] if 0 <= default_out < len(devices) else "нет"
        checks.append(
            Check(
                "audio",
                "ok" if inputs and outputs else "fail",
                f"вход: {len(inputs)} (по умолчанию {in_name}); "
                f"выход: {len(outputs)} (по умолчанию {out_name})",
                None if inputs and outputs else "Проверьте микрофон и динамики в настройках ОС",
            )
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("audio", "fail", f"PortAudio: {exc}", "Установите драйверы аудио"))

    # A short recording catches the most common macOS/Windows problem:
    # the OS silently returns zeros until microphone permission is granted.
    # It also protects the CLI from a wedged CoreAudio/PortAudio device: the
    # probe runs in a daemon thread and is abandoned after 2.5 seconds.
    try:
        import threading

        import numpy as np
        import sounddevice as sd

        result: dict[str, object] = {}

        def probe() -> None:
            try:
                sample_rate = 16000
                recording = sd.rec(
                    int(0.6 * sample_rate),
                    samplerate=sample_rate,
                    channels=1,
                    dtype="float32",
                )
                sd.wait()
                audio = np.asarray(recording, dtype=np.float64)
                rms = float(np.sqrt(np.mean(np.square(audio))))
                if np.isfinite(rms):
                    result["rms"] = rms
                else:
                    result["error"] = "микрофон вернул некорректный сигнал"
            except Exception as exc:  # noqa: BLE001
                result["error"] = str(exc)

        thread = threading.Thread(target=probe, name="jarvis-mic-probe", daemon=True)
        thread.start()
        thread.join(2.5)
        if thread.is_alive():
            checks.append(
                Check(
                    "mic signal",
                    "warn",
                    "микрофон не отвечает (занят или нет прав)",
                    "Закройте приложения, использующие микрофон, и проверьте "
                    "разрешения ОС. Перезапуск coreaudiod/системы обычно помогает.",
                )
            )
        elif "error" in result:
            checks.append(Check("mic signal", "warn", str(result["error"])))
        elif float(result.get("rms", 0.0)) <= 1e-7:
            checks.append(
                Check(
                    "mic signal",
                    "warn",
                    "микрофон возвращает тишину",
                    "Разрешите доступ к микрофону: macOS — System Settings → "
                    "Privacy & Security → Microphone; Windows — Settings → Privacy → Microphone",
                )
            )
        else:
            checks.append(Check("mic signal", "ok", f"RMS {float(result['rms']):.4f}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("mic signal", "warn", f"не удалось проверить: {exc}"))

    # TTS --------------------------------------------------------------------
    piper_voice = (
        config.tts.ru.voice if config.tts.ru.engine == "piper" else "ru_RU-ruslan-medium"
    )
    piper_model = paths.voices_dir / f"{piper_voice}.onnx"
    piper_detail = f"{piper_voice} — " + (
        "модель есть" if piper_model.exists() else "модель скачается при первом синтезе"
    )
    if not _has_module("piper"):
        checks.append(Check("piper", "fail", "пакет не установлен", "uv sync --all-groups"))
    else:
        checks.append(
            Check(
                "piper",
                "ok" if piper_model.exists() else "warn",
                piper_detail,
                None if piper_model.exists() else "jarvis voices install ru",
            )
        )

    kokoro_voice = (
        config.tts.en.voice if config.tts.en.engine == "kokoro" else "bm_george"
    )
    kokoro_model = paths.models_dir / "kokoro-v1.0.onnx"
    kokoro_detail = f"{kokoro_voice} — " + (
        "модель есть" if kokoro_model.exists() else "модель скачается при первом синтезе"
    )
    if not _has_module("kokoro_onnx"):
        checks.append(Check("kokoro", "fail", "пакет не установлен", "uv sync --all-groups"))
    else:
        checks.append(
            Check(
                "kokoro",
                "ok" if kokoro_model.exists() else "warn",
                kokoro_detail,
                None if kokoro_model.exists() else "jarvis voices install en",
            )
        )

    if "omnivoice_cpp" in {config.tts.ru.engine, config.tts.en.engine}:
        from .tts.omnivoice_cpp import OmniVoiceCppTTS
        from .tts.voice_import import list_assets

        ready, reason = OmniVoiceCppTTS.available(
            paths,
            config.tts.omnivoice_cpp,
            config.tts.ru.voice
            if config.tts.ru.engine == "omnivoice_cpp"
            else config.tts.en.voice,
        )
        checks.append(
            Check(
                "omnivoice.cpp",
                "ok" if ready else "warn",
                reason,
                None
                if ready
                else "jarvis voices install --engine omnivoice-cpp && ./install.sh",
            )
        )
        assets = list_assets(paths)
        checks.append(
            Check(
                "clone voice",
                "ok" if assets else "warn",
                ", ".join(asset.name for asset in assets) if assets else "голос не импортирован",
                None
                if assets
                else "jarvis voices import --name JARVIS --from assets "
                "(или --from file --audio ... --text ...)",
            )
        )

    from .tts.say import SayTTS

    checks.append(
        Check(
            "say",
            "ok" if SayTTS.available() else "warn",
            "системный синтезатор доступен" if SayTTS.available() else "недоступен",
            None if SayTTS.available() else "Резервный вариант будет недоступен",
        )
    )

    # STT --------------------------------------------------------------------
    if not _has_module("faster_whisper"):
        checks.append(Check("stt", "fail", "faster-whisper не установлен", "uv sync --all-groups"))
    else:
        whisper_cache = paths.cache_dir / "whisper"
        cached = whisper_cache.exists() and any(whisper_cache.iterdir())
        checks.append(
            Check(
                "stt",
                "ok" if cached else "warn",
                f"{config.stt.model} — "
                + ("модель в кэше" if cached else "скачается при первой транскрипции"),
                None if cached else "Первая транскрипция будет дольше",
            )
        )

    # Wake word --------------------------------------------------------------
    if config.wake.engine == "vad-whisper":
        if not _has_module("faster_whisper"):
            checks.append(
                Check("wake", "fail", "faster-whisper не установлен", "uv sync --all-groups")
            )
        else:
            checks.append(
                Check(
                    "wake",
                    "ok",
                    f"vad-whisper, «{config.wake.phrase}», {config.wake.stt_model} "
                    f"({config.wake.language}), VAD {config.wake.vad_mode}"
                    + (", fuzzy" if config.wake.fuzzy else ""),
                )
            )
    elif config.wake.engine == "openwakeword":
        if not _has_module("openwakeword"):
            checks.append(
                Check("wake", "fail", "openwakeword не установлен", "uv sync --all-groups")
            )
        else:
            import openwakeword

            model = Path(openwakeword.__file__).parent / "resources/models/hey_jarvis_v0.1.onnx"
            phrase = config.wake.phrase.strip().lower()
            english_only = phrase in {"hey jarvis", "hey jarvis!"}
            checks.append(
                Check(
                    "wake",
                    "ok" if model.exists() and english_only else "warn",
                    f"openwakeword, «{config.wake.phrase}» (порог {config.wake.threshold})",
                    None
                    if model.exists() and english_only
                    else "openWakeWord понимает только «Hey Jarvis». "
                    "Для «Джарвис»/«Jarvis» поставьте wake.engine = \"vad-whisper\"",
                )
            )
    elif config.wake.engine == "porcupine":
        has_key = bool(config.wake.access_key or os.environ.get("PICOVOICE_ACCESS_KEY"))
        keyword = Path(config.wake.keyword_path).expanduser() if config.wake.keyword_path else None
        has_keyword = bool(keyword and keyword.exists())
        ok = _has_module("pvporcupine") and has_key and has_keyword
        checks.append(
            Check(
                "wake",
                "ok" if ok else "warn",
                "porcupine, "
                + ("ключ есть" if has_key else "ключ не задан")
                + (", keyword есть" if has_keyword else ", keyword не задан"),
                None if ok else "wake.access_key + wake.keyword_path (русский .ppn)",
            )
        )
    else:
        checks.append(Check("wake", "fail", f"неизвестный движок: {config.wake.engine}"))

    # Disk / platform --------------------------------------------------------
    try:
        usage = shutil.disk_usage(paths.data_dir)
        free_gb = usage.free / (1024**3)
        checks.append(
            Check(
                "disk",
                "ok" if free_gb >= 1.0 else "warn",
                f"{free_gb:.1f} ГБ свободно в {paths.data_dir}",
                None if free_gb >= 1.0 else "Освободите место для моделей (~1–2 ГБ)",
            )
        )
    except OSError as exc:
        checks.append(Check("disk", "warn", str(exc)))

    checks.append(
        Check(
            "platform",
            "ok",
            f"{platform.system()} {platform.release()} / {platform.machine()}",
        )
    )
    checks.append(
        Check(
            "gui",
            "ok",
            "API готов; GUI выключен в конфиге" if not config.gui.enabled else "GUI включён",
        )
    )
    return checks


def checks_as_json(checks: list[Check]) -> str:
    return json.dumps([check.to_dict() for check in checks], ensure_ascii=False, indent=2)
