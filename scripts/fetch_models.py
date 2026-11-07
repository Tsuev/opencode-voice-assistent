#!/usr/bin/env python3
"""Download OmniVoice.cpp GGUF weights with resume, checksum and offline mode."""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

from rich.console import Console

from jarvis.installer import InstallError, fetch_omnivoice_models
from jarvis.paths import get_paths

console = Console()
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(errors="replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default="omnivoice-cpp", choices=["omnivoice-cpp"])
    parser.add_argument("--model", "--variant", dest="variant", default="q8", choices=["q8", "q4"])
    parser.add_argument("--models-dir", type=Path, default=None)
    parser.add_argument("--offline", action="store_true", help="не ходить в сеть")
    parser.add_argument("--accept-license", action="store_true", help="принять CC BY-NC 4.0")
    parser.add_argument("--force", action="store_true", help="перекачать даже готовые файлы")
    parser.add_argument("--hf-endpoint", default=None, help="например https://hf-mirror.com")
    args = parser.parse_args(argv)

    paths = get_paths().ensure()
    try:
        base, codec = fetch_omnivoice_models(
            paths,
            variant=args.variant,
            directory=args.models_dir,
            offline=args.offline,
            accept_license=args.accept_license,
            force=args.force,
            hf_endpoint=args.hf_endpoint,
            console=console,
        )
    except InstallError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    console.print(f"[green]base:[/green] {base}")
    console.print(f"[green]codec:[/green] {codec}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
