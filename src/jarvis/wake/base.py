"""Wake-word detector interface and factory."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import numpy as np

from ..config import SttConfig, VadConfig, WakeConfig


class WakeDetector(Protocol):
    name: str
    frame_size: int
    last_score: float
    last_text: str

    def process(self, frame: np.ndarray) -> bool: ...

    def reset(self) -> None: ...


def create_detector(
    config: WakeConfig,
    *,
    console=None,
    cache_dir: Path | None = None,
    stt_config: SttConfig | None = None,
    vad_config: VadConfig | None = None,
) -> WakeDetector:
    if config.engine == "vad-whisper":
        from .vad_whisper import VadWhisperDetector

        return VadWhisperDetector(
            phrase=config.phrase,
            model=config.stt_model,
            language=config.language,
            device=config.stt_device,
            compute_type=config.stt_compute_type,
            cache_dir=cache_dir,
            vad_mode=config.vad_mode,
            pre_roll_ms=config.pre_roll_ms,
            silence_ms=config.silence_ms,
            min_speech_ms=config.min_speech_ms,
            max_segment_s=config.max_segment_s,
            debounce=config.debounce,
            threshold=config.threshold,
            fuzzy=config.fuzzy,
            fuzzy_distance=config.fuzzy_distance,
            hotwords=config.hotwords,
            debug=config.debug,
            console=console,
        )
    if config.engine == "openwakeword":
        from .openwakeword import OpenWakeWordDetector

        return OpenWakeWordDetector(
            threshold=config.threshold,
            model_path=config.model_path,
            debounce=config.debounce,
            phrase=config.phrase,
        )
    if config.engine == "porcupine":
        from .porcupine import PorcupineDetector

        return PorcupineDetector(
            access_key=config.access_key,
            phrase=config.phrase,
            keyword_path=config.keyword_path,
            model_path=config.model_path,
            sensitivity=config.sensitivity,
            console=console,
        )
    raise NotImplementedError(f"Неизвестный wake-движок: {config.engine}")
