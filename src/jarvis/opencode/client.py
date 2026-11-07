"""Async client for the OpenCode v2 HTTP API.

Two modes:

* ``spawn``  - start a dedicated ``opencode serve`` child process and read its
  password from stdout (default; keeps voice sessions away from the TUI);
* ``attach`` - reuse the background service discovered via
  ``opencode service status`` and ``~/.config/opencode/service.json``.

Only the endpoints needed by the voice loop are wrapped.  Everything is
deliberately thin so a future API change stays local to this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from typing import Any

import httpx

from ..config import OpencodeConfig

log = logging.getLogger(__name__)

AUTH_USER = "opencode"

#: Events that matter to the voice pipeline.  Sending a narrow list keeps the
#: SSE stream quiet and avoids parsing reasoning/tool-input deltas.
DEFAULT_EVENT_TYPES: tuple[str, ...] = (
    "session.execution.started",
    "session.execution.succeeded",
    "session.execution.failed",
    "session.execution.interrupted",
    "session.text.started",
    "session.text.delta",
    "session.text.ended",
    "session.tool.called",
    "session.tool.success",
    "session.tool.failed",
    "permission.asked",
    "permission.replied",
)

_PASSWORD_RE = re.compile(r"server password\s+(\S+)", re.IGNORECASE)
_URL_RE = re.compile(r"(https?://\S+)")


class OpencodeError(RuntimeError):
    """Any failure while talking to OpenCode."""


@dataclass(slots=True)
class ServerHandle:
    url: str
    password: str
    process: subprocess.Popen[str] | None = None


def list_models(binary: str = "opencode") -> list[str]:
    """Return ``provider/model`` ids using the OpenCode CLI."""
    path = shutil.which(binary)
    if path is None:
        return []
    try:
        result = subprocess.run(
            [path, "models"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [
        line.strip()
        for line in (result.stdout or "").splitlines()
        if "/" in line and not line.startswith((" ", "\t"))
    ]


def parse_model_ref(value: str | None) -> dict[str, str] | None:
    """``provider/model#variant`` -> API model reference."""
    if not value:
        return None
    raw = value.strip()
    variant: str | None = None
    if "#" in raw:
        raw, variant = raw.split("#", 1)
    if "/" not in raw:
        raise ValueError(f"Модель должна быть в формате provider/model, получено: {value!r}")
    provider, model = raw.split("/", 1)
    if not provider or not model:
        raise ValueError(f"Некорректная модель: {value!r}")
    result = {"providerID": provider, "id": model}
    if variant:
        result["variant"] = variant
    return result


def _free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _read_lines(
    process: subprocess.Popen[str], queue: Queue[str], stop_event: threading.Event
) -> None:
    """Read the server banner, then keep draining stdout without growing memory."""
    assert process.stdout is not None
    for line in process.stdout:
        if stop_event.is_set():
            continue
        queue.put(line.rstrip("\n"))
    queue.put("__EOF__")


class OpencodeClient:
    def __init__(self, config: OpencodeConfig, *, console=None) -> None:
        self.config = config
        self.console = console
        self._http: httpx.AsyncClient | None = None
        self._handle: ServerHandle | None = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def __aenter__(self) -> OpencodeClient:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._http is not None:
            return
        binary = shutil.which(self.config.binary)
        if binary is None:
            raise OpencodeError(
                f"opencode не найден в PATH (binary={self.config.binary!r}). "
                "Установите OpenCode или укажите opencode.binary в конфиге."
            )

        if self.config.mode == "attach":
            handle = await asyncio.to_thread(self._attach, binary)
        else:
            handle = await self._spawn(binary)

        self._handle = handle
        self._http = httpx.AsyncClient(
            base_url=handle.url,
            auth=(AUTH_USER, handle.password),
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0),
        )
        try:
            await self.info()
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        if self._handle and self._handle.process is not None:
            process = self._handle.process
            if process.poll() is None:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, 5)
                except subprocess.TimeoutExpired:
                    process.kill()
            if process.stdout is not None:
                with contextlib.suppress(Exception):
                    process.stdout.close()
        self._handle = None

    @property
    def base_url(self) -> str:
        if not self._handle:
            raise OpencodeError("Клиент OpenCode не запущен")
        return self._handle.url

    @property
    def http(self) -> httpx.AsyncClient:
        if not self._http:
            raise OpencodeError("Клиент OpenCode не запущен")
        return self._http

    # ------------------------------------------------------------------
    # discovery
    # ------------------------------------------------------------------
    def _attach(self, binary: str) -> ServerHandle:
        try:
            result = subprocess.run(
                [binary, "service", "status"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise OpencodeError(f"Не удалось проверить сервис OpenCode: {exc}") from exc

        match = _URL_RE.search(result.stdout or "")
        if not match:
            raise OpencodeError(
                "Фоновый сервис OpenCode не запущен. "
                "Запустите `opencode service start` или переключите opencode.mode=spawn."
            )
        url = match.group(1).rstrip("/")

        from .agent import opencode_config_dir

        service_file = opencode_config_dir() / "service.json"
        if not service_file.exists():
            raise OpencodeError(f"Не найден {service_file} с паролем сервиса")
        try:
            password = json.loads(service_file.read_text(encoding="utf-8"))["password"]
        except (OSError, KeyError, json.JSONDecodeError) as exc:
            raise OpencodeError(f"Не удалось прочитать пароль из {service_file}: {exc}") from exc
        return ServerHandle(url=url, password=password)

    async def _spawn(self, binary: str) -> ServerHandle:
        host = self.config.host
        port = self.config.port or _free_port(host)
        cmd = [binary, "serve", "--hostname", host, "--port", str(port)]
        log.debug("spawning: %s", " ".join(cmd))
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        queue: Queue[str] = Queue()
        stop_reader = threading.Event()
        threading.Thread(
            target=_read_lines,
            args=(process, queue, stop_reader),
            name="opencode-stdout",
            daemon=True,
        ).start()

        password: str | None = None
        url: str | None = None
        deadline = time.monotonic() + self.config.startup_timeout
        output: list[str] = []
        try:
            while time.monotonic() < deadline:
                try:
                    line = await asyncio.to_thread(queue.get, True, 0.2)
                except Empty:
                    if process.poll() is not None:
                        break
                    continue
                if line == "__EOF__":
                    break
                output.append(line)
                if match := _PASSWORD_RE.search(line):
                    password = match.group(1)
                if match := _URL_RE.search(line):
                    url = match.group(1).rstrip("/")
                if password and url:
                    break
        finally:
            stop_reader.set()

        if password is None:
            process.terminate()
            tail = "\n".join(output[-10:]) or "<no output>"
            raise OpencodeError(f"OpenCode не сообщил пароль сервера.\n{tail}")

        if url is None:
            url = f"http://{host}:{port}"
        return ServerHandle(url=url, password=password, process=process)

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------
    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self.http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise OpencodeError(f"{method} {path}: {exc}") from exc
        if response.status_code >= 400:
            detail = response.text[:800]
            raise OpencodeError(f"{method} {path} -> HTTP {response.status_code}: {detail}")
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def info(self) -> dict[str, Any]:
        data = await self._request("GET", "/api/info")
        return data if isinstance(data, dict) else {"data": data}

    async def create_session(
        self,
        *,
        title: str = "Jarvis voice",
        directory: str | Path | None = None,
        agent: str | None = None,
        model: str | None = None,
        permissions: list[dict[str, str]] | None = None,
    ) -> str:
        body: dict[str, Any] = {"title": title}
        if agent:
            body["agent"] = agent
        model_ref = parse_model_ref(model)
        if model_ref:
            body["model"] = model_ref
        target = directory or self.config.directory
        if target:
            body["location"] = {"directory": str(Path(target).expanduser().resolve())}
        if permissions:
            body["permissions"] = permissions

        payload = await self._request("POST", "/api/session", json=body)
        try:
            return str(payload["data"]["id"])
        except (KeyError, TypeError) as exc:
            raise OpencodeError(f"Неожиданный ответ create_session: {payload!r}") from exc

    async def prompt(self, session_id: str, text: str) -> dict[str, Any]:
        payload = await self._request(
            "POST", f"/api/session/{session_id}/prompt", json={"text": text}
        )
        return payload or {}

    async def interrupt(self, session_id: str) -> None:
        await self._request("POST", f"/api/session/{session_id}/interrupt")

    async def reply_permission(self, session_id: str, request_id: str, decision: str) -> None:
        if decision not in {"once", "always", "reject"}:
            raise ValueError(f"Некорректное решение: {decision}")
        await self._request(
            "POST",
            f"/api/session/{session_id}/permission/{request_id}/reply",
            json={"decision": decision},
        )

    async def messages(self, session_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET", f"/api/session/{session_id}/message", params={"limit": limit}
        )
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        return list(data) if isinstance(data, list) else []

    async def list_models(self) -> list[str]:
        """Use the CLI because it already formats provider/model ids."""
        return await asyncio.to_thread(list_models, self.config.binary)

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------
    @asynccontextmanager
    async def events(
        self, types: tuple[str, ...] | None = None
    ) -> AsyncIterator[AsyncIterator[dict[str, Any]]]:
        from .sse import iter_sse

        params = [("types", event_type) for event_type in (types or DEFAULT_EVENT_TYPES)]
        try:
            async with self.http.stream(
                "GET", "/api/event", params=params, timeout=None
            ) as response:
                if response.status_code >= 400:
                    detail = (await response.aread()).decode("utf-8", "replace")[:800]
                    raise OpencodeError(f"GET /api/event -> HTTP {response.status_code}: {detail}")

                async def iterator() -> AsyncIterator[dict[str, Any]]:
                    async for sse in iter_sse(response.aiter_lines()):
                        try:
                            yield sse.json()
                        except json.JSONDecodeError:
                            log.debug("bad SSE payload: %s", sse.data[:200])

                yield iterator()
        except httpx.HTTPError as exc:
            raise OpencodeError(f"Ошибка потока событий: {exc}") from exc

    async def stream_turn(
        self,
        session_id: str,
        text: str,
        *,
        types: tuple[str, ...] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Prompt and yield only events belonging to this session's turn."""
        async with self.events(types) as stream:
            # Let the SSE request establish itself before the first event can fire.
            await asyncio.sleep(0.05)
            await self.prompt(session_id, text)
            try:
                async with asyncio.timeout(self.config.turn_timeout):
                    async for payload in stream:
                        data = payload.get("data") or {}
                        if data.get("sessionID") not in (None, session_id):
                            continue
                        yield payload
                        if payload.get("type") in {
                            "session.execution.succeeded",
                            "session.execution.failed",
                            "session.execution.interrupted",
                        }:
                            return
            except TimeoutError as exc:
                raise OpencodeError(
                    f"OpenCode не ответил за {self.config.turn_timeout:.0f} с"
                ) from exc
