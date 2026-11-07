"""Common TTS types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass(slots=True)
class Audio:
    """Mono float32 PCM in ``[-1, 1]``."""

    samples: np.ndarray
    sample_rate: int

    def __post_init__(self) -> None:
        if self.samples.ndim > 1:
            self.samples = self.samples.reshape(-1)
        if self.samples.dtype != np.float32:
            self.samples = self.samples.astype(np.float32, copy=False)
        if self.samples.size:
            peak = float(np.max(np.abs(self.samples)))
            if peak > 1.0:
                self.samples = self.samples / peak

    @property
    def duration(self) -> float:
        if not self.sample_rate:
            return 0.0
        return self.samples.size / self.sample_rate

    @property
    def empty(self) -> bool:
        return self.samples.size == 0


class TTSProvider(Protocol):
    name: str

    async def synthesize(self, text: str, voice: str, speed: float = 1.0) -> Audio: ...
