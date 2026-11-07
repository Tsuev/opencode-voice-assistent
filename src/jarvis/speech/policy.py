"""Rules that turn OpenCode output into short, speakable sentences.

OpenCode is a coding agent: raw assistant text contains code fences, diffs,
URLs, tables and file paths.  Speaking all of that is unbearable, so the
policy is deliberately aggressive:

* only assistant ``text`` parts ever reach this module (never ``tool`` or
  ``reasoning`` parts);
* code, links, tables and markdown noise are removed;
* the final answer is capped by characters and split at sentence boundaries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`]*`")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+|www\.\S+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_BULLET = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_BOLD_ITALIC = re.compile(r"(\*\*|__|\*|_)(?=\S)")
_SPACE = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{2,}")
_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+|\n+")
_CLAUSE_SPLIT = re.compile(r"(?<=[,;:])\s+")

#: Spoken instead of a truncated tail.  Kept short on purpose.
TRUNCATION_NOTE = "Подробности в терминале."


@dataclass(slots=True)
class SpeechPlan:
    chunks: list[str]
    original_chars: int
    spoken_chars: int
    truncated: bool = False
    dropped: bool = False
    meta: dict[str, object] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return " ".join(self.chunks)


def sanitize(text: str) -> str:
    """Remove markup and other things that sound terrible when spoken."""
    if not text:
        return ""
    result = _ANSI.sub("", text)
    result = _FENCED_CODE.sub(" ", result)
    result = _IMAGE.sub(" ", result)
    result = _LINK.sub(r"\1", result)
    result = _INLINE_CODE.sub(" ", result)
    result = _URL.sub(" ", result)
    result = _TABLE_LINE.sub("", result)
    result = _HEADING.sub("", result)
    result = _BULLET.sub("", result)
    result = _BOLD_ITALIC.sub("", result)
    result = result.replace("|", " ")
    result = _SPACE.sub(" ", result)
    result = _BLANK_LINES.sub("\n", result)
    return result.strip()


def _split_long(sentence: str, limit: int) -> list[str]:
    if len(sentence) <= limit:
        return [sentence]
    parts: list[str] = []
    current = ""
    for clause in _CLAUSE_SPLIT.split(sentence):
        candidate = f"{current} {clause}".strip()
        if current and len(candidate) > limit:
            parts.append(current)
            current = clause
        else:
            current = candidate
    if current:
        parts.append(current)

    result: list[str] = []
    for part in parts:
        if len(part) <= limit:
            result.append(part)
            continue
        words = part.split()
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if current and len(candidate) > limit:
                result.append(current)
                current = word
            else:
                current = candidate
        if current:
            result.append(current)
    return result


def chunk_text(text: str, max_sentence_chars: int = 180) -> list[str]:
    """Split sanitized text into sentence-sized TTS chunks."""
    chunks: list[str] = []
    for sentence in _SENTENCE_SPLIT.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        chunks.extend(_split_long(sentence, max_sentence_chars))
    return chunks


def prepare(
    text: str,
    *,
    max_chars: int = 400,
    max_sentence_chars: int = 180,
    truncation_note: str = TRUNCATION_NOTE,
) -> SpeechPlan:
    """Build a speech plan from one assistant text block."""
    original_chars = len(text)
    clean = sanitize(text)
    if not clean:
        return SpeechPlan([], original_chars, 0, dropped=True)

    truncated = len(clean) > max_chars
    if truncated:
        cut = clean[:max_chars]
        boundary = max(cut.rfind(" "), cut.rfind("\n"), cut.rfind("."))
        if boundary > max_chars * 0.5:
            cut = cut[:boundary]
        clean = cut.rstrip(" ,;:-")

    chunks = chunk_text(clean, max_sentence_chars)
    if truncated and truncation_note:
        chunks.append(truncation_note)

    return SpeechPlan(
        chunks=chunks,
        original_chars=original_chars,
        spoken_chars=sum(len(chunk) for chunk in chunks),
        truncated=truncated,
    )


def normalize_wake_text(text: str) -> str:
    """Lowercase and strip punctuation for wake-word matching."""
    return re.sub(r"[^\w\sа-яё]", "", text.lower(), flags=re.IGNORECASE).strip()


def strip_wake_word(text: str, phrases: list[str]) -> str:
    """Remove a leading wake phrase from a transcript."""
    normalized = normalize_wake_text(text)
    for phrase in phrases:
        normalized_phrase = normalize_wake_text(phrase)
        if normalized.startswith(normalized_phrase):
            # Remove by character count on the original text, not on the
            # normalized copy, to preserve the rest of the sentence.
            words = text.split()
            phrase_words = len(phrase.split())
            remainder = " ".join(words[phrase_words:]).lstrip(" ,.!?-—")
            return remainder or text
    return text


def _levenshtein(left: str, right: str, *, limit: int) -> int:
    """Damerau-Levenshtein distance with an early exit above ``limit``.

    Adjacent transpositions count as one edit: Whisper often writes "Джаврис"
    instead of "Джарвис", and that should still wake the assistant.
    """
    if left == right:
        return 0
    if abs(len(left) - len(right)) > limit:
        return limit + 1
    previous: list[int] = list(range(len(right) + 1))
    previous_previous: list[int] | None = None
    for i in range(1, len(left) + 1):
        current = [i]
        row_min = i
        for j in range(1, len(right) + 1):
            cost = 0 if left[i - 1] == right[j - 1] else 1
            value = min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + cost,
            )
            if (
                previous_previous is not None
                and i > 1
                and j > 1
                and left[i - 1] == right[j - 2]
                and left[i - 2] == right[j - 1]
            ):
                value = min(value, previous_previous[j - 2] + 1)
            current.append(value)
            row_min = min(row_min, value)
        if row_min > limit:
            return limit + 1
        previous_previous, previous = previous, current
    return previous[-1]


def _fuzzy_phrase_match(text_tokens: list[str], phrase_tokens: list[str], limit: int) -> bool:
    if not phrase_tokens or len(phrase_tokens) > len(text_tokens):
        return False
    width = len(phrase_tokens)
    for start in range(len(text_tokens) - width + 1):
        window = text_tokens[start : start + width]
        total = 0
        for spoken, expected in zip(window, phrase_tokens, strict=True):
            # One-letter words are too ambiguous for fuzzy matching.
            if len(expected) < 3 or len(spoken) < 3:
                if spoken != expected:
                    total = limit + 1
                    break
                continue
            distance = _levenshtein(spoken, expected, limit=limit)
            total += distance
            if total > limit:
                break
        if total <= limit:
            return True
    return False


def contains_wake_word(
    text: str,
    phrases: list[str],
    *,
    fuzzy: bool = False,
    max_distance: int = 1,
) -> bool:
    normalized = f" {normalize_wake_text(text)} "
    for phrase in phrases:
        normalized_phrase = normalize_wake_text(phrase)
        if not normalized_phrase:
            continue
        if f" {normalized_phrase} " in normalized:
            return True
        if fuzzy and max_distance > 0:
            text_tokens = normalize_wake_text(text).split()
            phrase_tokens = normalized_phrase.split()
            if _fuzzy_phrase_match(text_tokens, phrase_tokens, max_distance):
                return True
    return False
