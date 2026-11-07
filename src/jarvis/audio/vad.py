"""Voice activity detection.

WebRTC VAD is tiny, fast and works on Windows/macOS/Linux.  It expects 10/20/30
ms PCM16 frames, so we split our 80 ms capture blocks into 20 ms sub-frames and
use a majority vote.
"""

from __future__ import annotations

import numpy as np


class WebRtcVad:
    def __init__(self, mode: int = 2, sample_rate: int = 16000, frame_ms: int = 20) -> None:
        try:
            import webrtcvad
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("webrtcvad-wheels не установлен: uv add webrtcvad-wheels") from exc

        if mode not in range(4):
            raise ValueError("VAD mode должен быть 0..3")
        self._vad = webrtcvad.Vad(mode)
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.frame_samples = int(sample_rate * frame_ms / 1000)

    def _chunks(self, frame: np.ndarray) -> list[np.ndarray]:
        frame = np.asarray(frame, dtype=np.int16).reshape(-1)
        size = self.frame_samples
        return [frame[i : i + size] for i in range(0, len(frame) - size + 1, size)]

    def is_speech(self, frame: np.ndarray) -> bool:
        chunks = self._chunks(frame)
        if not chunks:
            return False
        votes = sum(
            1
            for chunk in chunks
            if len(chunk) == self.frame_samples
            and self._vad.is_speech(chunk.tobytes(), self.sample_rate)
        )
        return votes >= max(1, len(chunks) // 2)

    def speech_ratio(self, frame: np.ndarray) -> float:
        chunks = self._chunks(frame)
        if not chunks:
            return 0.0
        votes = sum(
            1
            for chunk in chunks
            if len(chunk) == self.frame_samples
            and self._vad.is_speech(chunk.tobytes(), self.sample_rate)
        )
        return votes / len(chunks)
