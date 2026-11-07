from __future__ import annotations

from jarvis.speech.streaming import SpeechStreamer


async def test_streams_sentences_as_they_arrive() -> None:
    spoken: list[str] = []

    async def speak(text: str) -> None:
        spoken.append(text)

    streamer = SpeechStreamer(speak)
    streamer.feed_delta("Открываю Steam. ")
    streamer.end_part("Открываю Steam. Готово.")
    error = await streamer.finish()

    assert error is None
    assert spoken == ["Открываю Steam.", "Готово."]
    assert streamer.spoken_chars == sum(len(item) for item in spoken)


async def test_full_text_is_used_when_no_deltas() -> None:
    spoken: list[str] = []

    async def speak(text: str) -> None:
        spoken.append(text)

    streamer = SpeechStreamer(speak)
    streamer.end_part("Системы онлайн.")
    error = await streamer.finish()

    assert error is None
    assert spoken == ["Системы онлайн."]


async def test_error_is_reported_once() -> None:
    spoken: list[str] = []

    async def speak(text: str) -> None:
        spoken.append(text)
        raise RuntimeError("no audio")

    streamer = SpeechStreamer(speak)
    streamer.feed_delta("Первое. Второе. Третье.")
    error = await streamer.finish()

    assert isinstance(error, RuntimeError)
    assert spoken == ["Первое."]
    assert streamer.spoken_chars == 0
