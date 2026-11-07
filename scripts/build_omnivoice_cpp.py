#!/usr/bin/env python3
"""Build omnivoice.cpp and install the ``tts-server`` binary for Jarvis."""

from __future__ import annotations

import argparse
import contextlib
import sys

from rich.console import Console

from jarvis.installer import InstallError, binary_path, build_omnivoice_cpp
from jarvis.paths import get_paths

console = Console()
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(errors="replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cuda", action="store_true", help="собрать с CUDA")
    parser.add_argument("--vulkan", action="store_true", help="собрать с Vulkan")
    parser.add_argument("--update", action="store_true", help="git pull перед сборкой")
    parser.add_argument("--force", action="store_true", help="удалить старый бинарник")
    args = parser.parse_args(argv)

    paths = get_paths().ensure()
    destination = binary_path(paths)
    if args.force and destination.exists():
        destination.unlink()
    try:
        built = build_omnivoice_cpp(
            paths,
            cuda=args.cuda,
            vulkan=args.vulkan,
            update=args.update,
            console=console,
        )
    except InstallError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    console.print(f"[green]Готово:[/green] {built}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
