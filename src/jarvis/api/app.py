"""Local HTTP API for the CLI, plugins and the future GUI.

Contract (also documented in ``docs/gui.md``):

* ``GET  /api/health``   - liveness
* ``GET  /api/state``    - full state snapshot (JSON)
* ``GET  /api/events``   - Server-Sent Events stream
* ``POST /api/ask``      - text in, assistant reply out
* ``POST /api/speak``    - speak arbitrary text (used by OpenCode plugins)
* ``POST /api/listen``   - enable/disable the microphone
* ``POST /api/session/new``
* ``GET  /api/models``   - models known to OpenCode
* ``GET  /api/voices``   - local TTS catalogue
* ``GET  /api/config``   - sanitized configuration

Authentication is a bearer token from the Jarvis config directory
(``jarvis init`` prints the exact path).
"""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..opencode.client import list_models
from ..runtime import Runtime
from ..tts.catalog import ALL_VOICES


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    language: str | None = None


class ListenRequest(BaseModel):
    enabled: bool = True


class AskRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    speak: bool = False


def create_app(runtime: Runtime) -> FastAPI:
    app = FastAPI(
        title="Jarvis API",
        version=__version__,
        description="Local voice assistant for OpenCode",
    )

    origins = runtime.config.api.cors_origins
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    def require_token(request: Request) -> None:
        expected = runtime.config.api.token
        if not expected:
            return
        header = request.headers.get("authorization", "")
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(value, expected):
            raise HTTPException(
                status_code=401,
                detail="Invalid API token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    auth = [Depends(require_token)]

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    @app.get("/api/state", dependencies=auth)
    async def state() -> dict[str, Any]:
        return runtime.snapshot()

    @app.get("/api/config", dependencies=auth)
    async def config() -> dict[str, Any]:
        payload = runtime.config.model_dump(mode="json", exclude_none=True)
        payload.get("api", {}).pop("token", None)
        return payload

    @app.get("/api/voices", dependencies=auth)
    async def voices() -> dict[str, Any]:
        from ..tts.voice_import import list_assets

        items = [
            {
                "engine": voice.engine,
                "name": voice.name,
                "language": voice.language,
                "gender": voice.gender,
                "description": voice.description,
            }
            for voice in ALL_VOICES
        ]
        known = {item["name"] for item in items}
        paths = getattr(runtime, "paths", None)
        if paths is not None:
            for asset in list_assets(paths):
                if asset.name in known:
                    continue
                items.append(
                    {
                        "engine": "clone",
                        "name": asset.name,
                        "language": "ru+en",
                        "gender": "clone",
                        "description": f"Импортирован ({asset.profile.get('source', 'file')})",
                    }
                )
        return {"voices": items}

    @app.get("/api/models", dependencies=auth)
    async def models() -> dict[str, Any]:
        return {"models": await asyncio.to_thread(list_models, runtime.config.opencode.binary)}

    @app.post("/api/speak", dependencies=auth)
    async def speak(request: SpeakRequest) -> dict[str, Any]:
        await runtime.speak(request.text, language=request.language)
        return {"ok": True}

    @app.post("/api/listen", dependencies=auth)
    async def listen(request: ListenRequest) -> dict[str, Any]:
        await runtime.set_listening(request.enabled)
        return {"ok": True, "listening": request.enabled}

    @app.post("/api/ask", dependencies=auth)
    async def ask(request: AskRequest) -> dict[str, Any]:
        reply = await runtime.ask(request.text, speak=request.speak)
        return {"reply": reply}

    @app.post("/api/session/new", dependencies=auth)
    async def new_session() -> dict[str, Any]:
        session_id = await runtime.new_session()
        return {"session_id": session_id}

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        # EventSource cannot set headers, so accept the token as a query param
        # for the future browser GUI as well.
        expected = runtime.config.api.token
        if expected:
            supplied = request.query_params.get("token") or request.headers.get(
                "authorization", ""
            ).removeprefix("Bearer ")
            if not supplied or not secrets.compare_digest(supplied, expected):
                raise HTTPException(status_code=401, detail="Invalid API token")

        async def stream():
            subscription = runtime.bus.subscribe()
            try:
                hello = {"type": "hello", "ts": 0, "data": runtime.snapshot()}
                yield f"event: hello\ndata: {json.dumps(hello, ensure_ascii=False)}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(subscription.__anext__(), timeout=15)
                    except TimeoutError:
                        yield ": ping\n\n"
                        continue
                    except StopAsyncIteration:
                        break
                    yield f"data: {json.dumps(event.to_dict(), ensure_ascii=False)}\n\n"
            finally:
                runtime.bus.unsubscribe(subscription)
                await subscription.aclose()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app
