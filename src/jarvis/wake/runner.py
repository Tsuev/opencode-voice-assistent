"""Wake-word detection loop."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from ..audio.input import Microphone
from .base import WakeDetector


@dataclass(frozen=True, slots=True)
class WakeDetection:
    engine: str
    phrase: str
    score: float
    ts: float


class WakeRunner:
    def __init__(
        self,
        detector: WakeDetector,
        microphone: Microphone,
        *,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.detector = detector
        self.microphone = microphone
        self.stop_event = stop_event or threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    def run(
        self, on_detect: Callable[[WakeDetection], None] | None = None
    ) -> Iterator[WakeDetection]:
        for frame in self.microphone.frames(self.stop_event):
            if self.detector.process(frame):
                detection = WakeDetection(
                    engine=self.detector.name,
                    phrase=getattr(self.detector, "phrase", "jarvis"),
                    score=self.detector.last_score,
                    ts=time.time(),
                )
                if on_detect is not None:
                    on_detect(detection)
                yield detection
