"""Speaker playback.

``sounddevice.wait()`` can block forever when CoreAudio/WASAPI gets wedged
(usually after a permission change or a device switch).  The player therefore
polls the stream and aborts with a clear error instead of hanging the assistant.
"""

from __future__ import annotations

import time

import numpy as np

from ..tts.base import Audio


class PlaybackTimeout(RuntimeError):
    pass


def play_audio(audio: Audio, device: str | int | None = None, *, blocking: bool = True) -> None:
    import sounddevice as sd

    if audio.empty:
        return
    sd.play(
        np.ascontiguousarray(audio.samples, dtype=np.float32),
        audio.sample_rate,
        device=device,
    )
    if not blocking:
        return

    stream = sd.get_stream()
    if stream is None:
        return

    deadline = time.monotonic() + max(2.0, audio.duration + 5.0)
    while stream.active:
        if time.monotonic() >= deadline:
            try:
                sd.stop()
            finally:
                raise PlaybackTimeout(
                    "Аудиоустройство не отвечает. Проверьте динамики и права "
                    "доступа; при зависании CoreAudio поможет перезапуск системы."
                )
        time.sleep(0.05)


def stop_audio() -> None:
    import sounddevice as sd

    sd.stop()
