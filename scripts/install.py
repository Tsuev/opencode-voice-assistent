#!/usr/bin/env python3
"""One-shot Jarvis installer: models, TTS server, cloned voice, config."""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

from rich.console import Console

from jarvis.installer import InstallError, install
from jarvis.tts.voice_import import VoiceImportError

console = Console()
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(errors="replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default="omnivoice-cpp", choices=["omnivoice-cpp"])
    parser.add_argument(
        "--model", "--variant", dest="variant", default="q8", choices=["q8", "q4"]
    )
    parser.add_argument("--models-dir", type=Path, default=None)
    parser.add_argument("--offline", action="store_true", help="не ходить в сеть")
    parser.add_argument("--accept-license", action="store_true", help="принять CC BY-NC 4.0")
    parser.add_argument(
        "--build-from-source",
        action="store_true",
        help="собрать tts-server, а не искать prebuilt",
    )
    parser.add_argument("--cuda", action="store_true", help="собрать с CUDA (Windows/Linux)")
    parser.add_argument("--vulkan", action="store_true", help="собрать с Vulkan")
    parser.add_argument("--cpu", action="store_true", help="собрать CPU-версию (медленно)")
    parser.add_argument("--voice-name", default="JARVIS")
    parser.add_argument(
        "--voice-source",
        default="voicestudio",
        choices=["voicestudio", "file"],
    )
    parser.add_argument("--voice-audio", type=Path, default=None)
    parser.add_argument("--voice-text", default=None)
    parser.add_argument("--no-voice-import", action="store_true")
    parser.add_argument("--lang", default="all", choices=["ru", "en", "all", "both"])
    parser.add_argument("--skip-warmup", action="store_true")
    args = parser.parse_args(argv)

    try:
        install(
            variant=args.variant,
            engine=args.engine,
            offline=args.offline,
            accept_license=args.accept_license,
            models=args.models_dir,
            build=args.build_from_source,
            cuda=args.cuda,
            vulkan=args.vulkan,
            force_cpu=args.cpu,
            voice_name=args.voice_name,
            voice_source=args.voice_source,
            voice_audio=args.voice_audio,
            voice_text=args.voice_text,
            no_voice=args.no_voice_import,
            language=args.lang,
            warmup=not args.skip_warmup,
            console=console,
        )
    except (InstallError, VoiceImportError) as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
