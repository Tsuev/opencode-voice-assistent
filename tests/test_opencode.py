from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from jarvis.opencode.client import parse_model_ref
from jarvis.opencode.sse import iter_sse


def test_parse_model_ref() -> None:
    assert parse_model_ref("local-deepseek/deepseek-chat") == {
        "providerID": "local-deepseek",
        "id": "deepseek-chat",
    }
    assert parse_model_ref("openai/gpt-5#high") == {
        "providerID": "openai",
        "id": "gpt-5",
        "variant": "high",
    }
    assert parse_model_ref("") is None
    with pytest.raises(ValueError):
        parse_model_ref("no-slash")


async def _lines() -> AsyncIterator[str]:
    for line in [
        "event: hello",
        'data: {"a": 1}',
        "",
        ": ping",
        "",
        "data: first",
        "data: second",
        "",
    ]:
        yield line


async def test_iter_sse() -> None:
    events = [event async for event in iter_sse(_lines())]
    assert len(events) == 2
    assert events[0].event == "hello"
    assert events[0].json() == {"a": 1}
    assert events[1].data == "first\nsecond"
