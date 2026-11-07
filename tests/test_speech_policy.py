from __future__ import annotations

from jarvis.speech.language import detect_language
from jarvis.speech.policy import (
    contains_wake_word,
    prepare,
    sanitize,
    strip_wake_word,
)


def test_sanitize_removes_code_and_links() -> None:
    raw = """Вот результат:

```python
print("hello")
```

Ссылка: https://example.com/page и [документация](https://example.com/docs).
| a | b |
| - | - |
"""
    clean = sanitize(raw)
    assert "print" not in clean
    assert "https://" not in clean
    assert "документация" in clean
    assert "|" not in clean


def test_prepare_splits_and_limits() -> None:
    plan = prepare(
        "Первое предложение. Второе предложение! Третье?",
        max_chars=100,
        max_sentence_chars=40,
    )
    assert plan.chunks == ["Первое предложение.", "Второе предложение!", "Третье?"]
    assert not plan.truncated


def test_prepare_truncates_with_note() -> None:
    long_text = "Очень длинная фраза. " * 50
    plan = prepare(long_text, max_chars=100, max_sentence_chars=80)
    assert plan.truncated
    assert plan.chunks[-1] == "Подробности в терминале."
    assert plan.spoken_chars > 100


def test_sanitize_empty_code_only_is_dropped() -> None:
    plan = prepare("```bash\nrm -rf /\n```")
    assert plan.dropped
    assert plan.chunks == []


def test_language_detection() -> None:
    assert detect_language("Открой калькулятор") == "ru"
    assert detect_language("Open the calculator") == "en"
    assert detect_language("12345") == "unknown"
    assert detect_language("Открой Steam") == "ru"


def test_wake_word_helpers() -> None:
    assert contains_wake_word("эй, Джарвис, открой Steam", ["джарвис"])
    assert not contains_wake_word("открой Steam", ["джарвис"])
    assert strip_wake_word("Джарвис, открой Steam", ["джарвис"]) == "открой Steam"
    assert (
        strip_wake_word("Хей Джарвис, открой Steam", ["хей джарвис", "джарвис"]) == "открой Steam"
    )
    assert strip_wake_word("Эй Джарвис, открой Steam", ["эй джарвис", "джарвис"]) == "открой Steam"
