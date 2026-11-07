"""Wake-word detector interface and factory."""

from __future__ import annotations

from typing import Protocol

import numpy as np

from ..config import WakeConfig


class WakeDetector(Protocol):
    name: str
    frame_size: int
    last_score: float

    def process(self, frame: np.ndarray) -> bool: ...

    def reset(self) -> None: ...


def create_detector(config: WakeConfig, *, console=None) -> WakeDetector:
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
            console=console,
        )
    raise NotImplementedError(
        "Движок vad-whisper появится в P2. Сейчас используйте openwakeword или porcupine."
    )
