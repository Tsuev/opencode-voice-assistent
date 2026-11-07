from __future__ import annotations

from jarvis.opencode.agent import agent_source


def test_russian_agent_rule() -> None:
    text = agent_source("ru")
    assert "по-русски" in text
    assert "{{LANGUAGE_RULE}}" not in text
    assert "Always answer in English" not in text


def test_english_agent_rule() -> None:
    text = agent_source("en")
    assert "Always answer in English" in text
    assert "{{LANGUAGE_RULE}}" not in text
    assert "по-русски" not in text
