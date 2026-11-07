from __future__ import annotations

import numpy as np

from jarvis.wake.vad_whisper import VadWhisperDetector


class _SequenceVad:
    def __init__(self, values: list[bool]) -> None:
        self._values = list(values)

    def is_speech(self, frame: np.ndarray) -> bool:
        return self._values.pop(0)


def _frame(value: int = 0, samples: int = 1280) -> np.ndarray:
    return np.full(samples, value, dtype=np.int16)


def test_vad_whisper_detects_russian_wake_word_and_keeps_command() -> None:
    seen: list[int] = []

    def transcribe(audio: np.ndarray) -> str:
        seen.append(int(audio.size))
        return "Джарвис, открой калькулятор."

    detector = VadWhisperDetector(
        phrase="джарвис",
        pre_roll_ms=0,
        silence_ms=160,
        min_speech_ms=80,
        vad=_SequenceVad([True, True, False, False]),
        transcriber=transcribe,
    )

    hits = [detector.process(_frame(100)) for _ in range(4)]

    assert hits == [False, False, False, True]
    assert detector.last_text == "Джарвис, открой калькулятор."
    assert detector.last_audio.size > 0
    assert seen and seen[0] > 0


def test_vad_whisper_ignores_other_speech() -> None:
    detector = VadWhisperDetector(
        phrase="джарвис",
        pre_roll_ms=0,
        silence_ms=160,
        min_speech_ms=80,
        vad=_SequenceVad([True, True, False, False]),
        transcriber=lambda _audio: "Сегодня хорошая погода.",
    )

    hits = [detector.process(_frame(100)) for _ in range(4)]

    assert hits == [False, False, False, False]
    assert detector.last_text == ""


def test_vad_whisper_accepts_fuzzy_russian_wake_word() -> None:
    detector = VadWhisperDetector(
        phrase="джарвис",
        pre_roll_ms=0,
        silence_ms=160,
        min_speech_ms=80,
        vad=_SequenceVad([True, False, False]),
        transcriber=lambda _audio: "Джаврис!",
    )

    assert detector.process(_frame(100)) is False
    assert detector.process(_frame(0)) is False
    assert detector.process(_frame(0)) is True
    assert detector.last_text == "Джаврис!"


def test_vad_whisper_accepts_hey_jarvis_variant() -> None:
    detector = VadWhisperDetector(
        pre_roll_ms=0,
        silence_ms=160,
        min_speech_ms=80,
        vad=_SequenceVad([True, False, False]),
        transcriber=lambda _audio: "Эй, Джарвис!",
    )

    assert detector.process(_frame(100)) is False
    assert detector.process(_frame(0)) is False
    assert detector.process(_frame(0)) is True
    assert detector.last_text
