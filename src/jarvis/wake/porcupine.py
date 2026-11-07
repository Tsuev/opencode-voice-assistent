"""Optional Porcupine adapter (better accuracy, proprietary SDK).

The built-in keywords are English.  For the Russian "Джарвис" create a custom
keyword in https://console.picovoice.ai/ (language: Russian), download the
platform ``.ppn`` and point ``wake.keyword_path`` at it.  Porcupine's Russian
model ships inside ``pvporcupine``.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np

NAMED_KEYWORDS = {"jarvis", "alexa", "computer", "hey google", "ok google"}


def _russian_model_path() -> Path | None:
    try:
        import pvporcupine
    except ImportError:  # pragma: no cover - checked by the caller
        return None
    root = Path(pvporcupine.__file__).resolve().parent
    candidates = [
        root / "lib" / "common" / "porcupine_params_ru.pv",
        root / "resources" / "porcupine_params_ru.pv",
        root / "porcupine_params_ru.pv",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    matches = list(root.rglob("porcupine_params_ru.pv"))
    return matches[0] if matches else None


class PorcupineDetector:
    name = "porcupine"
    frame_size = 512  # Porcupine's native frame length at 16 kHz

    def __init__(
        self,
        *,
        access_key: str | None = None,
        phrase: str = "джарвис",
        keyword_path: str | None = None,
        model_path: str | None = None,
        sensitivity: float = 0.6,
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

        self.phrase = phrase
        keyword = phrase.lower().strip()
        if keyword_path:
            path = Path(keyword_path).expanduser()
            if not path.exists():
                raise FileNotFoundError(f"Custom keyword Porcupine не найден: {path}")
            resolved_model = (
                Path(model_path).expanduser()
                if model_path
                else _russian_model_path()
            )
            if resolved_model is None or not resolved_model.exists():
                raise FileNotFoundError(
                    "Не найдена русская модель Porcupine (porcupine_params_ru.pv). "
                    "Переустановите pvporcupine или укажите wake.model_path."
                )
            self.model_path = resolved_model
            self._porcupine = pvporcupine.create(
                access_key=key,
                keyword_paths=[str(path)],
                model_path=str(resolved_model),
                sensitivities=[float(sensitivity)],
            )
        else:
            if keyword not in NAMED_KEYWORDS:
                raise ValueError(
                    f"Для фразы «{phrase}» нужен custom keyword Porcupine. "
                    "Создайте его в Picovoice Console на русском и укажите "
                    "wake.keyword_path. Встроенные английские слова: "
                    f"{', '.join(sorted(NAMED_KEYWORDS))}."
                )
            self.model_path = Path(model_path).expanduser() if model_path else Path("builtin")
            kwargs = {
                "access_key": key,
                "keywords": [keyword],
                "sensitivities": [float(sensitivity)],
            }
            if model_path:
                kwargs["model_path"] = str(self.model_path)
            self._porcupine = pvporcupine.create(**kwargs)
        self.frame_size = self._porcupine.frame_length
        self.last_score = 0.0
        self.last_text = ""
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
                self.last_score = 1.0
        return detected

    def reset(self) -> None:
        self._buffer = np.zeros(0, dtype=np.int16)
        self.last_score = 0.0
        self.last_text = ""
