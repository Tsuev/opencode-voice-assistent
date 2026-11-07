"""Logging setup shared by CLI and daemon."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

_LEVELS = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
    "trace": logging.DEBUG,
}


def setup_logging(
    level: str = "info",
    log_file: Path | None = None,
    *,
    console: Console | None = None,
) -> Console:
    """Configure root logging with a rich console and optional rotating file."""
    console = console or Console(stderr=True)
    resolved = _LEVELS.get(level.lower(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(resolved)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    console_handler = RichHandler(
        console=console,
        show_path=False,
        show_time=False,
        rich_tracebacks=True,
        markup=True,
    )
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(console_handler)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
        root.addHandler(file_handler)

    for noisy in (
        "httpx",
        "httpcore",
        "httpx2",
        "httpcore2",
        "urllib3",
        "huggingface_hub",
        "hf_xet",
        "faster_whisper",
        "onnxruntime",
        "phonemizer",
        "kokoro_onnx",
        "espeakng_loader",
        "transformers",
        "torch",
        "omnivoice",
        "pvporcupine",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return console
