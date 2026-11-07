"""Curated voice catalogue.

We intentionally keep a short list of voices that are known to work well for a
calm, butler-like delivery.  Advanced users can still put any Piper voice name
into the config.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VoiceInfo:
    engine: str
    name: str
    language: str
    gender: str
    description: str


PIPER_VOICES: tuple[VoiceInfo, ...] = (
    VoiceInfo("piper", "ru_RU-ruslan-medium", "ru", "male", "Спокойный русский мужской"),
    VoiceInfo("piper", "ru_RU-denis-medium", "ru", "male", "Русский мужской, чуть ниже"),
    VoiceInfo("piper", "ru_RU-dmitri-medium", "ru", "male", "Русский мужской, размеренный"),
    VoiceInfo("piper", "ru_RU-irina-medium", "ru", "female", "Русский женский"),
    VoiceInfo("piper", "en_GB-alan-medium", "en", "male", "Британский мужской"),
    VoiceInfo("piper", "en_GB-northern_english_male-medium", "en", "male", "Североанглийский"),
    VoiceInfo("piper", "en_US-ryan-high", "en", "male", "Американский мужской"),
)

KOKORO_VOICES: tuple[VoiceInfo, ...] = (
    VoiceInfo("kokoro", "bm_george", "en", "male", "Британский батлер — ближе всего к Jarvis"),
    VoiceInfo("kokoro", "bm_fable", "en", "male", "Британский, мягче и старше"),
    VoiceInfo("kokoro", "bm_lewis", "en", "male", "Британский, суховатый"),
    VoiceInfo("kokoro", "am_michael", "en", "male", "Американский мужской"),
    VoiceInfo("kokoro", "af_heart", "en", "female", "Американский женский"),
)

CLONE_VOICES: tuple[VoiceInfo, ...] = (
    VoiceInfo(
        "omnivoice_cpp",
        "jarvis",
        "ru+en",
        "clone",
        "Твой клон JARVIS (OmniVoice.cpp, GGUF, ~1 ГБ)",
    ),
)

ALL_VOICES: tuple[VoiceInfo, ...] = PIPER_VOICES + KOKORO_VOICES + CLONE_VOICES


def voices_for(engine: str | None = None, language: str | None = None) -> list[VoiceInfo]:
    result = list(ALL_VOICES)
    if engine:
        result = [voice for voice in result if voice.engine == engine]
    if language:
        result = [voice for voice in result if language in voice.language.split("+")]
    return result


def find_voice(name: str) -> VoiceInfo | None:
    for voice in ALL_VOICES:
        if voice.name == name:
            return voice
    return None
