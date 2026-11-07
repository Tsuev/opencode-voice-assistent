"""The GUI contract is tested without audio hardware or a real OpenCode."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from jarvis.api.app import create_app
from jarvis.config import JarvisConfig
from jarvis.events import EventBus


class FakeRuntime:
    def __init__(self) -> None:
        self.config = JarvisConfig()
        self.config.api.token = "test-token"
        self.bus = EventBus()
        self.spoken: list[str] = []
        self.listening = True
        self.session_id: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": {"state": "idle", "wake_enabled": self.listening},
            "config": {},
            "session_id": self.session_id,
            "listening": self.listening,
        }

    async def speak(self, text: str, language: str | None = None) -> None:
        self.spoken.append(text)

    async def set_listening(self, enabled: bool) -> None:
        self.listening = enabled

    async def ask(self, text: str, speak: bool = False) -> str:
        return f"echo: {text}"

    async def new_session(self) -> str:
        self.session_id = "ses_test"
        return self.session_id


def _client() -> tuple[TestClient, FakeRuntime]:
    runtime = FakeRuntime()
    return TestClient(create_app(runtime)), runtime


def test_health_is_public() -> None:
    client, _ = _client()
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_state_requires_token() -> None:
    client, _ = _client()
    assert client.get("/api/state").status_code == 401
    response = client.get("/api/state", headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 200
    assert response.json()["state"]["state"] == "idle"


def test_speak_and_listen() -> None:
    client, runtime = _client()
    headers = {"Authorization": "Bearer test-token"}

    response = client.post("/api/speak", json={"text": "Проверка"}, headers=headers)
    assert response.status_code == 200
    assert runtime.spoken == ["Проверка"]

    response = client.post("/api/listen", json={"enabled": False}, headers=headers)
    assert response.status_code == 200
    assert runtime.listening is False


def test_ask() -> None:
    client, _ = _client()
    response = client.post(
        "/api/ask",
        json={"text": "привет", "speak": False},
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    assert response.json()["reply"] == "echo: привет"


def test_new_session() -> None:
    client, runtime = _client()
    response = client.post("/api/session/new", headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 200
    assert response.json()["session_id"] == "ses_test"
    assert runtime.session_id == "ses_test"
