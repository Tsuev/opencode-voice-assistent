"""Model downloads with progress and atomic writes."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

KOKORO_MODEL_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/kokoro-v1.0.onnx"
)
KOKORO_VOICES_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"
)

KOKORO_MODEL_FILE = "kokoro-v1.0.onnx"
KOKORO_VOICES_FILE = "voices-v1.0.bin"


class DownloadError(RuntimeError):
    pass


def download_file(
    url: str,
    destination: Path,
    *,
    label: str | None = None,
    console: Console | None = None,
    show_progress: bool = True,
) -> Path:
    """Download ``url`` to ``destination`` atomically, skipping existing files."""
    destination = destination.expanduser()
    if destination.exists() and destination.stat().st_size > 0:
        return destination

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    if partial.exists():
        partial.unlink()

    label = label or destination.name
    timeout = httpx.Timeout(connect=20.0, read=120.0, write=60.0, pool=20.0)

    with httpx.stream("GET", url, follow_redirects=True, timeout=timeout) as response:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise DownloadError(
                f"Не удалось скачать {label}: HTTP {exc.response.status_code}"
            ) from exc

        total = int(response.headers.get("content-length", 0) or 0)
        if not show_progress or console is None:
            with partial.open("wb") as handle:
                for chunk in response.iter_bytes(1024 * 256):
                    handle.write(chunk)
        else:
            with Progress(
                TextColumn("[bold]{task.description}"),
                BarColumn(),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                console=console,
                transient=True,
            ) as progress:
                task = progress.add_task(label, total=total or None)
                with partial.open("wb") as handle:
                    for chunk in response.iter_bytes(1024 * 256):
                        handle.write(chunk)
                        progress.update(task, advance=len(chunk))

    os.replace(partial, destination)
    return destination


def ensure_kokoro(models_dir: Path, *, console: Console | None = None) -> tuple[Path, Path]:
    """Return ``(model_path, voices_path)``, downloading if missing."""
    model = download_file(
        KOKORO_MODEL_URL,
        models_dir / KOKORO_MODEL_FILE,
        label="Kokoro model (~325 MB)",
        console=console,
    )
    voices = download_file(
        KOKORO_VOICES_URL,
        models_dir / KOKORO_VOICES_FILE,
        label="Kokoro voices (~28 MB)",
        console=console,
    )
    return model, voices


def ensure_piper_voice(voice: str, voices_dir: Path, *, console: Console | None = None) -> Path:
    """Download a Piper voice into ``voices_dir`` and return the ``.onnx`` path."""
    voices_dir.mkdir(parents=True, exist_ok=True)
    model_path = voices_dir / f"{voice}.onnx"
    if model_path.exists() and model_path.stat().st_size > 0:
        return model_path

    try:
        from piper.download_voices import download_voice
    except ImportError as exc:  # pragma: no cover - dependency is in pyproject
        raise DownloadError("piper-tts не установлен") from exc

    if console is not None:
        console.print(f"[dim]Скачиваю голос Piper: {voice}[/dim]")
    try:
        download_voice(voice, voices_dir)
    except Exception as exc:  # noqa: BLE001 - library raises different types
        raise DownloadError(f"Не удалось скачать голос {voice}: {exc}") from exc

    if not model_path.exists():
        candidates = sorted(voices_dir.glob(f"{voice}*.onnx"))
        if not candidates:
            raise DownloadError(f"Голос {voice} не появился в {voices_dir}")
        candidates[0].rename(model_path)
    return model_path
