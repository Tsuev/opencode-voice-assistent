from __future__ import annotations

import numpy as np

from jarvis.audio.vad import WebRtcVad


def test_silence_is_not_speech() -> None:
    vad = WebRtcVad(mode=2)
    assert not vad.is_speech(np.zeros(1280, dtype=np.int16))


def test_short_frame_is_ignored() -> None:
    vad = WebRtcVad(mode=2)
    assert not vad.is_speech(np.zeros(10, dtype=np.int16))
