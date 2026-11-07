"""Optional Porcupine adapter (better accuracy, proprietary SDK).

Requires a free Picovoice access key.  The built-in ``jarvis`` keyword is
included in the SDK, so no custom model training is needed.
"""

from __future__ import annotations

import os

import numpy as np

NAMED_KEYWORDS = {"jarvis", "alexa", "computer", "hey google", "ok google"}


class PorcupineDetector:
    name = "porcupine"
    frame_size = 512  # Porcupine's native frame length at 16 kHz

    def __init__(
        self,
        *,
        access_key: str | None = None,
        phrase: str = "jarvis",
        sensitivity: float = 0.5,
        console=None,
    ) -> None:
        try:
            import pvporcupine
        except ImportError as exc:
            raise RuntimeError("Porcupine не установлен. Выполните: uv add pvporcupine") from exc

        key = access_key or os.environ.get("PICOVOICE_ACCESS_KEY")
        if not key:
            raise RuntimeError(
                "Для Porcupine нужен бесплатный Picovoice Access Key. "
                "Получите его на https://console.picovoice.ai/ и укажите "
                "wake.access_key или переменную PICOVOICE_ACCESS_KEY."
            )

        keyword = phrase.lower().strip()
        if keyword not in NAMED_KEYWORDS:
            raise ValueError(
                f"Встроенное слово «{phrase}» не поддерживается Porcupine. "
                f"Доступны: {', '.join(sorted(NAMED_KEYWORDS))}."
            )

        self._porcupine = pvporcupine.create(
            access_key=key,
            keywords=[keyword],
            sensitivities=[sensitivity],
        )
        self.frame_size = self._porcupine.frame_length
        self.last_score = 0.0
        self._buffer = np.zeros(0, dtype=np.int16)
        self._console = console

    def process(self, frame: np.ndarray) -> bool:
        self._buffer = np.concatenate([self._buffer, frame.astype(np.int16, copy=False)])
        detected = False
        while self._buffer.size >= self.frame_size:
            chunk = self._buffer[: self.frame_size]
            self._buffer = self._buffer[self.frame_size :]
            if self._porcupine.process(chunk) >= 0:
                detected = True
        return detected

    def reset(self) -> None:
        self._buffer = np.zeros(0, dtype=np.int16)
