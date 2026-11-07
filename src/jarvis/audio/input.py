"""Microphone capture with automatic sample-rate conversion.

openWakeWord and Whisper both want 16 kHz mono int16.  Some Windows/WASAPI
devices refuse to open at 16 kHz, so we fall back to the device's native rate
and resample every block with soxr.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import numpy as np


class Microphone:
    def __init__(
        self,
        device: str | int | None = None,
        *,
        sample_rate: int = 16000,
        block_ms: int = 80,
        channels: int = 1,
    ) -> None:
        self.device = device
        self.sample_rate = sample_rate
        self.block_ms = block_ms
        self.channels = channels

    @contextmanager
    def open_stream(self):
        import sounddevice as sd

        target_rate = self.sample_rate
        try:
            stream = sd.RawInputStream(
                samplerate=target_rate,
                blocksize=int(target_rate * self.block_ms / 1000),
                device=self.device,
                dtype="int16",
                channels=self.channels,
            )
            stream.start()
            stream_rate = target_rate
        except Exception:
            # Fall back to the device's native rate (common on Windows WASAPI).
            info = sd.query_devices(self.device, "input")
            stream_rate = int(info["default_samplerate"])
            stream = sd.RawInputStream(
                samplerate=stream_rate,
                blocksize=int(stream_rate * self.block_ms / 1000),
                device=self.device,
                dtype="int16",
                channels=self.channels,
            )
            stream.start()

        try:
            yield stream, stream_rate
        finally:
            stream.stop()
            stream.close()

    def frames(self, stop_event: threading.Event | None = None) -> Iterator[np.ndarray]:
        """Yield 16 kHz int16 frames of ``block_ms`` milliseconds."""
        with self.frames_stream() as iterator:
            for frame in iterator:
                if stop_event and stop_event.is_set():
                    break
                yield frame

    @contextmanager
    def frames_stream(self) -> Iterator[Iterator[np.ndarray]]:
        """Open the microphone and yield a resampled frame iterator.

        The stream stays open while the iterator is consumed, which lets the
        voice loop switch from wake word to command recording without glitches.
        """
        import soxr

        with self.open_stream() as (stream, stream_rate):
            input_block = int(stream_rate * self.block_ms / 1000)
            resampler = (
                soxr.ResampleStream(stream_rate, self.sample_rate, self.channels, dtype="float32")
                if stream_rate != self.sample_rate
                else None
            )

            def iterator() -> Iterator[np.ndarray]:
                while True:
                    data, overflowed = stream.read(input_block)
                    del overflowed
                    frame = np.frombuffer(data, dtype=np.int16).copy()
                    if resampler is not None:
                        floats = frame.astype(np.float32) / 32768.0
                        if self.channels > 1:
                            floats = floats.reshape(-1, self.channels)
                        resampled = resampler.resample_chunk(floats, last=False)
                        if self.channels > 1:
                            resampled = resampled.reshape(-1)
                        frame = np.clip(resampled * 32767.0, -32768, 32767).astype(np.int16)
                    yield frame

            yield iterator()


def record_seconds(seconds: float, *, sample_rate: int = 16000, device=None) -> np.ndarray:
    """Record a fixed number of seconds (used by ``jarvis stt-test``)."""
    import sounddevice as sd

    frames = int(seconds * sample_rate)
    recording = sd.rec(frames, samplerate=sample_rate, channels=1, dtype="float32", device=device)
    sd.wait()
    return recording.reshape(-1)


def record_seconds_checked(
    seconds: float,
    *,
    sample_rate: int = 16000,
    device=None,
    timeout: float | None = None,
) -> np.ndarray:
    """Record with a watchdog so a wedged audio driver cannot hang the CLI."""
    result: dict[str, object] = {}

    def worker() -> None:
        try:
            result["audio"] = record_seconds(seconds, sample_rate=sample_rate, device=device)
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    thread = threading.Thread(target=worker, name="jarvis-record", daemon=True)
    thread.start()
    thread.join(timeout if timeout is not None else seconds + 5.0)
    if thread.is_alive():
        raise TimeoutError(
            "Микрофон не отвечает. Проверьте, не занят ли он другим приложением, "
            "и разрешения ОС на доступ к микрофону."
        )
    if "error" in result:
        raise result["error"]  # type: ignore[misc]
    return result["audio"]  # type: ignore[return-value]


class WavSource:
    """Deterministic microphone replacement for tests and ``--wav`` mode."""

    def __init__(self, path: str | Path, *, sample_rate: int = 16000, block_ms: int = 80) -> None:
        self.path = Path(path).expanduser()
        self.sample_rate = sample_rate
        self.block_ms = block_ms
        self._used = False

    @contextmanager
    def frames_stream(self) -> Iterator[Iterator[np.ndarray]]:
        if self._used:
            yield iter(())
            return
        self._used = True

        import soundfile as sf
        import soxr

        audio, sample_rate = sf.read(self.path, dtype="int16", always_2d=False)
        audio = np.asarray(audio, dtype=np.int16)
        if audio.ndim > 1:
            audio = audio[:, 0]
        if sample_rate != self.sample_rate:
            floats = audio.astype(np.float32) / 32768.0
            resampled = soxr.resample(floats, sample_rate, self.sample_rate)
            audio = np.clip(resampled * 32767.0, -32768, 32767).astype(np.int16)

        block = max(1, int(self.sample_rate * self.block_ms / 1000))

        def iterator() -> Iterator[np.ndarray]:
            for start in range(0, len(audio), block):
                yield audio[start : start + block]

        yield iterator()
