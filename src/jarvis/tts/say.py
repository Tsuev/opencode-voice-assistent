"""Zero-download fallback using the platform speech synthesizer.

macOS: ``say``
Windows: SAPI via PowerShell
Linux: ``espeak-ng`` (optional)
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from .base import Audio

_MAC_VOICES = {"en": "Daniel", "ru": "Milena"}
_WIN_VOICES = {"en": "Microsoft David Desktop", "ru": "Microsoft Irina Desktop"}


def _read_audio(path: Path) -> Audio:
    import soundfile as sf

    samples, sample_rate = sf.read(path, dtype="float32", always_2d=False)
    return Audio(np.asarray(samples, dtype=np.float32), int(sample_rate))


class SayTTS:
    name = "say"

    def __init__(self, voice: str = "", *, language: str = "en") -> None:
        self.voice = voice
        self.language = language

    @staticmethod
    def available() -> bool:
        system = platform.system()
        if system == "Darwin":
            return shutil.which("say") is not None
        if system == "Windows":
            return shutil.which("powershell") is not None or shutil.which("pwsh") is not None
        return shutil.which("espeak-ng") is not None

    def _synthesize_macos(self, text: str, speed: float) -> Audio:
        voice = self.voice or _MAC_VOICES.get(self.language, "Daniel")
        with tempfile.TemporaryDirectory(prefix="jarvis-say-") as tmp:
            out = Path(tmp) / "say.wav"
            rate = max(80, int(175 * speed))
            subprocess.run(
                [
                    "say",
                    "-v",
                    voice,
                    "-r",
                    str(rate),
                    "-o",
                    str(out),
                    "--file-format=WAVE",
                    "--data-format=LEI16@22050",
                    text,
                ],
                check=True,
                capture_output=True,
            )
            return _read_audio(out)

    def _synthesize_windows(self, text: str, speed: float) -> Audio:
        voice = self.voice or _WIN_VOICES.get(self.language, "")
        with tempfile.TemporaryDirectory(prefix="jarvis-say-") as tmp:
            out = Path(tmp) / "say.wav"
            script = (
                "Add-Type -AssemblyName System.Speech; "
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                f"$s.Rate = {max(-10, min(10, int((speed - 1.0) * 5)))}; "
                "if ($env:JARVIS_VOICE) { try { $s.SelectVoice($env:JARVIS_VOICE) } catch {} }; "
                "$s.SetOutputToWaveFile($env:JARVIS_OUT); "
                "$s.Speak($env:JARVIS_TEXT); "
                "$s.Dispose()"
            )
            env = {
                **os.environ,
                "JARVIS_TEXT": text,
                "JARVIS_OUT": str(out),
                "JARVIS_VOICE": voice,
            }
            shell = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
            subprocess.run(
                [shell, "-NoProfile", "-NonInteractive", "-Command", script],
                check=True,
                capture_output=True,
                env=env,
            )
            return _read_audio(out)

    def _synthesize_linux(self, text: str, speed: float) -> Audio:
        with tempfile.TemporaryDirectory(prefix="jarvis-say-") as tmp:
            out = Path(tmp) / "say.wav"
            voice = self.voice or ("ru" if self.language == "ru" else "en")
            subprocess.run(
                [
                    "espeak-ng",
                    "-v",
                    voice,
                    "-s",
                    str(max(80, int(175 * speed))),
                    "-w",
                    str(out),
                    text,
                ],
                check=True,
                capture_output=True,
            )
            return _read_audio(out)

    def _synthesize_sync(self, text: str, speed: float) -> Audio:
        system = platform.system()
        if system == "Darwin":
            return self._synthesize_macos(text, speed)
        if system == "Windows":
            return self._synthesize_windows(text, speed)
        return self._synthesize_linux(text, speed)

    async def synthesize(
        self,
        text: str,
        voice: str | None = None,
        speed: float = 1.0,
        language: str | None = None,
    ) -> Audio:
        if language in {"ru", "en"}:
            self.language = language
        return await asyncio.to_thread(self._synthesize_sync, text, speed)
