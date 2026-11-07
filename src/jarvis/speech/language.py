"""Cheap language routing for TTS voices.

We do not need a full language detector: replies are short and either
Cyrillic or Latin.  Mixed replies pick the dominant script.
"""

from __future__ import annotations

import re
from typing import Literal

Language = Literal["ru", "en", "unknown"]

_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_LATIN = re.compile(r"[A-Za-z]")


def detect_language(text: str) -> Language:
    cyrillic = len(_CYRILLIC.findall(text))
    latin = len(_LATIN.findall(text))
    if cyrillic == 0 and latin == 0:
        return "unknown"
    if cyrillic >= latin:
        return "ru"
    return "en"
