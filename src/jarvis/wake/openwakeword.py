"""openWakeWord adapter.

The ``hey_jarvis_v0.1.onnx`` model ships inside the pip package, so no account
or download is required.  Audio must be 16 kHz mono int16, 1280 samples
(80 ms) per prediction.
"""

from __future__ import annotations

import pathlib
import time
import warnings

import numpy as np

# onnxruntime warns about CUDA on machines without NVIDIA; harmless for us.
warnings.filterwarnings("ignore", message=".*CUDAExecutionProvider.*")

FRAME_SIZE = 1280


class OpenWakeWordDetector:
    name = "openwakeword"
    frame_size = FRAME_SIZE

    def __init__(
        self,
        *,
        threshold: float = 0.5,
        model_path: str | None = None,
        debounce: float = 1.2,
        phrase: str = "hey jarvis",
    ) -> None:
        with warnings.catch_warnings():
            # onnxruntime warns about CUDA on machines without NVIDIA; harmless.
            warnings.filterwarnings("ignore", message=".*CUDAExecutionProvider.*")
            from openwakeword.model import Model

        self.threshold = threshold
        self.debounce = debounce
        self.phrase = phrase
        self.last_score = 0.0
        self._cooldown_until = 0.0
        self._model_path = self._resolve_model(model_path)
        self._model = Model(wakeword_model_paths=[str(self._model_path)])
        self._model_key = next(iter(self._model.models), "hey_jarvis_v0.1")

    @staticmethod
    def _resolve_model(model_path: str | None) -> pathlib.Path:
        if model_path:
            path = pathlib.Path(model_path).expanduser()
            if not path.exists():
                raise FileNotFoundError(f"Модель wake word не найдена: {path}")
            return path
        import openwakeword

        bundled = (
            pathlib.Path(openwakeword.__file__).parent
            / "resources"
            / "models"
            / "hey_jarvis_v0.1.onnx"
        )
        if not bundled.exists():
            raise FileNotFoundError(
                "В пакете openwakeword нет hey_jarvis_v0.1.onnx. "
                "Переустановите openwakeword или укажите wake.model_path."
            )
        return bundled

    @property
    def model_path(self) -> pathlib.Path:
        return self._model_path

    def score(self, frame: np.ndarray) -> float:
        scores = self._model.predict(frame)
        self.last_score = float(scores.get(self._model_key, 0.0))
        return self.last_score

    def process(self, frame: np.ndarray) -> bool:
        now = time.monotonic()
        if now < self._cooldown_until:
            return False
        score = self.score(frame)
        if score >= self.threshold:
            self._cooldown_until = now + self.debounce
            self._model.reset()
            return True
        return False

    def reset(self) -> None:
        self._model.reset()
        self._cooldown_until = 0.0
