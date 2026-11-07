"""Cross-platform installer helpers.

``scripts/install.py`` and ``install.sh`` call into this module.  It keeps the
logic testable and avoids duplicating OS-specific code in shell.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

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

from .config import VoiceConfig, init_config, load_config, save_config
from .paths import AppPaths, get_paths
from .toolchain import Toolchain, detect_toolchain
from .tts.downloads import ensure_kokoro, ensure_piper_voice
from .tts.omnivoice_cpp import OmniVoiceCppTTS
from .tts.voice_import import (
    VoiceAsset,
    VoiceImportError,
    import_from_file,
    import_from_voicestudio,
    slugify,
)

ENGINE = "omnivoice-cpp"
DEFAULT_VARIANT = "q8"
DEFAULT_VOICE = "JARVIS"


class InstallError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# manifest / paths
# ---------------------------------------------------------------------------
def load_manifest() -> dict[str, Any]:
    resource = importlib.resources.files("jarvis.resources").joinpath("models.json")
    return json.loads(resource.read_text(encoding="utf-8"))


def engine_manifest() -> dict[str, Any]:
    manifest = load_manifest()
    try:
        return manifest[ENGINE]
    except KeyError as exc:  # pragma: no cover - manifest is shipped with us
        raise InstallError(f"В manifest нет движка {ENGINE}") from exc


def models_dir(paths: AppPaths, override: str | Path | None = None) -> Path:
    if override:
        return Path(override).expanduser().resolve()
    return paths.models_dir / "omnivoice-cpp"


def binary_name() -> str:
    return "tts-server.exe" if sys.platform == "win32" else "tts-server"


def binary_path(paths: AppPaths) -> Path:
    return paths.bin_dir / binary_name()


def platform_key() -> str:
    machine = platform.machine().lower()
    if sys.platform == "win32":
        return "windows-x86_64"
    if sys.platform == "darwin":
        return "macos-arm64" if machine in {"arm64", "aarch64"} else "macos-x86_64"
    return "linux-x86_64"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_matches(path: Path, size: int, sha256: str) -> bool:
    try:
        return path.stat().st_size == size and _sha256(path) == sha256
    except OSError:
        return False


# ---------------------------------------------------------------------------
# local-first resolution
# ---------------------------------------------------------------------------
def _local_candidates(filename: str, destination: Path) -> list[Path]:
    cwd = Path.cwd()
    candidates = [
        destination / filename,
        cwd / "models" / filename,
        cwd / "models" / "omnivoice-cpp" / filename,
    ]
    return [candidate for candidate in candidates if candidate.exists()]


def _adopt_local(source: Path, destination: Path, size: int, sha256: str) -> bool:
    if not _file_matches(source, size, sha256):
        return False
    if source.resolve() == destination.resolve():
        return True
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)
    return True


def resolve_or_copy(
    filename: str,
    *,
    destination: Path,
    size: int,
    sha256: str,
) -> Path | None:
    """Return an existing verified file, adopting it into ``destination``."""
    for candidate in _local_candidates(filename, destination):
        if _adopt_local(candidate, destination / filename, size, sha256):
            return destination / filename
    return None


# ---------------------------------------------------------------------------
# downloads
# ---------------------------------------------------------------------------
def _download_one(
    client: httpx.Client,
    url: str,
    destination: Path,
    *,
    size: int,
    sha256: str,
    force: bool,
    console: Console | None,
) -> Path:
    if not force and _file_matches(destination, size, sha256):
        if console:
            console.print(f"[dim]уже есть: {destination.name}[/dim]")
        return destination

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    if force and partial.exists():
        partial.unlink()
    offset = partial.stat().st_size if partial.exists() else 0
    if offset > size:
        partial.unlink()
        offset = 0

    headers = {"Range": f"bytes={offset}-"} if offset else {}
    label = destination.name
    if console:
        progress = Progress(
            TextColumn("[bold]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=True,
        )
        task = progress.add_task(label, total=size)
        progress.update(task, advance=offset)
        progress.start()
    else:  # pragma: no cover - headless path
        progress = None
        task = None

    try:
        with client.stream("GET", url, headers=headers, follow_redirects=True) as response:
            response.raise_for_status()
            mode = "ab" if response.status_code == 206 and offset else "wb"
            if mode == "wb":
                offset = 0
            with partial.open(mode) as handle:
                for chunk in response.iter_bytes(1024 * 256):
                    handle.write(chunk)
                    if progress is not None and task is not None:
                        progress.update(task, advance=len(chunk))
    finally:
        if progress is not None:
            progress.stop()

    if partial.stat().st_size != size:
        partial.unlink(missing_ok=True)
        raise InstallError(
            f"Неверный размер {label}: {partial.stat().st_size if partial.exists() else 0} "
            f"вместо {size}"
        )
    actual = _sha256(partial)
    if actual != sha256:
        partial.unlink(missing_ok=True)
        raise InstallError(f"SHA256 не совпал для {label}: {actual}")
    os.replace(partial, destination)
    if console:
        console.print(f"[green]скачано:[/green] {destination}")
    return destination


def _download_with_retry(
    client: httpx.Client,
    url: str,
    destination: Path,
    *,
    size: int,
    sha256: str,
    force: bool,
    console: Console | None,
) -> Path:
    last: Exception | None = None
    for attempt in range(1, 4):
        try:
            return _download_one(
                client,
                url,
                destination,
                size=size,
                sha256=sha256,
                force=force and attempt == 1,
                console=console,
            )
        except (httpx.HTTPError, OSError, InstallError) as exc:
            last = exc
            if console:
                console.print(f"[yellow]попытка {attempt}/3 не удалась: {exc}[/yellow]")
    raise InstallError(f"Не удалось скачать {destination.name}: {last}")


def _license_notice(console: Console | None) -> None:
    text = (
        "OmniVoice GGUF weights are licensed CC BY-NC 4.0.\n"
        "They may be used for non-commercial purposes only."
    )
    if console:
        console.print(text, style="yellow")


def _confirm_license(accept: bool, console: Console | None) -> None:
    _license_notice(console)
    if accept:
        return
    if sys.stdin.isatty():
        answer = input("Continue? [y/N] ").strip().lower()
        if answer in {"y", "yes", "д", "да"}:
            return
    raise InstallError(
        "Лицензия OmniVoice не принята. Повторите с --accept-license "
        "(веса CC BY-NC 4.0, только некоммерческое использование)."
    )


def fetch_omnivoice_models(
    paths: AppPaths,
    *,
    variant: str = DEFAULT_VARIANT,
    directory: str | Path | None = None,
    offline: bool = False,
    accept_license: bool = False,
    force: bool = False,
    hf_endpoint: str | None = None,
    console: Console | None = None,
    client: httpx.Client | None = None,
) -> tuple[Path, Path]:
    """Ensure the base model and codec GGUF are present.

    Resolution order is local-first: ``--models-dir``, ``./models``, the
    Jarvis cache, then Hugging Face.  ``--offline`` never touches the network.
    """
    variant = variant.lower().strip()
    meta = engine_manifest()
    variants = meta.get("variants", {})
    if variant not in variants:
        raise InstallError(
            f"Неизвестный вариант {variant!r}; доступны: {', '.join(variants)}"
        )
    destination = models_dir(paths, directory)
    destination.mkdir(parents=True, exist_ok=True)
    files = variants[variant]
    expected = [("base", files["base"]), ("tokenizer", files["tokenizer"])]

    resolved: dict[str, Path] = {}
    missing: list[tuple[str, dict[str, Any]]] = []
    for key, item in expected:
        found = resolve_or_copy(
            item["name"],
            destination=destination,
            size=int(item["size"]),
            sha256=item["sha256"],
        )
        if found is not None:
            resolved[key] = found
        else:
            missing.append((key, item))

    if missing and offline:
        names = ", ".join(item["name"] for _key, item in missing)
        raise InstallError(
            f"Offline-режим: не найдены {names}. Положите их в {destination} "
            "или укажите --models-dir."
        )

    if missing:
        _confirm_license(accept_license, console)
        endpoint = (
            hf_endpoint
            or os.environ.get("HF_ENDPOINT")
            or "https://huggingface.co"
        ).rstrip("/")
        repo = meta["repo"]
        revision = meta.get("revision", "main")
        owns_client = client is None
        client = client or httpx.Client(
            timeout=httpx.Timeout(connect=30.0, read=120.0, write=60.0, pool=30.0)
        )
        try:
            for key, item in missing:
                url = f"{endpoint}/{repo}/resolve/{revision}/{item['name']}"
                resolved[key] = _download_with_retry(
                    client,
                    url,
                    destination / item["name"],
                    size=int(item["size"]),
                    sha256=item["sha256"],
                    force=force,
                    console=console,
                )
        finally:
            if owns_client:
                client.close()

    base = resolved.get("base") or destination / files["base"]["name"]
    codec = resolved.get("tokenizer") or destination / files["tokenizer"]["name"]
    return base, codec


# ---------------------------------------------------------------------------
# binary
# ---------------------------------------------------------------------------
def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> None:
    result = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env)
    if result.returncode != 0:
        raise InstallError(f"Команда завершилась с кодом {result.returncode}: {' '.join(cmd)}")


def _format_toolchain(toolchain: Toolchain) -> str:
    return "\n".join(f"  {line}" for line in toolchain.summary())


def _find_built_binary(source_dir: Path) -> Path | None:
    candidates = [
        source_dir / "build" / "tts-server",
        source_dir / "build" / "tts-server.exe",
        source_dir / "build" / "Release" / "tts-server.exe",
        source_dir / "build" / "Release" / "tts-server",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    # Ninja/Multi-config layouts differ between upstream revisions.
    for name in ("tts-server.exe", "tts-server"):
        matches = sorted(source_dir.rglob(name))
        if matches:
            return matches[0]
    return None


def _copy_runtime_libs(build_dir: Path, built: Path, destination: Path) -> None:
    """Copy ggml backend libraries next to ``tts-server``.

    A shared ggml build produces ``ggml.dll``/``ggml-cuda.dll`` (or
    ``.so``/``.dylib``).  Copying only the executable leaves it unable to
    start outside the build directory.
    """
    seen: set[str] = set()
    for root in (built.parent, build_dir):
        if not root.exists():
            continue
        for pattern in ("*.dll", "*.so", "*.so.*", "*.dylib"):
            for library in root.glob(pattern):
                key = library.name.lower()
                if key in seen:
                    continue
                seen.add(key)
                target = destination.parent / library.name
                if library.resolve() == target.resolve():
                    continue
                shutil.copy2(library, target)


def _cmake_version_tuple(version: str | None) -> tuple[int, int]:
    if not version:
        return (0, 0)
    try:
        parts = version.split()[2].split(".")
        return (int(parts[0]), int(parts[1]))
    except (IndexError, ValueError):
        return (0, 0)


def build_omnivoice_cpp(
    paths: AppPaths,
    *,
    cuda: bool = False,
    vulkan: bool = False,
    update: bool = False,
    console: Console | None = None,
) -> Path:
    """Clone and build omnivoice.cpp, then install ``tts-server``.

    Tool discovery is deliberately independent of the current ``PATH``: CMake,
    CUDA and MSVC are often installed after the terminal was opened.  On Windows
    the MSVC environment is loaded from ``vcvars64.bat`` when necessary.
    """
    git = shutil.which("git")
    toolchain = detect_toolchain()
    cmake = toolchain.cmake
    if not git:
        raise InstallError("Для сборки нужен git")
    if cmake is None:
        raise InstallError(
            "CMake не найден. Он может быть установлен, но не виден текущему "
            "терминалу. Проверьте `cmake --version` в новом терминале или "
            "установите: Windows — `winget install Kitware.CMake`, "
            "macOS — `brew install cmake`.\n"
            f"Найденные инструменты:\n{_format_toolchain(toolchain)}"
        )
    if sys.platform == "win32" and not toolchain.has_compiler and not toolchain.gpp:
        raise InstallError(
            "CMake найден, но компилятор C++ не найден. Установите "
            "Visual Studio 2022 Build Tools с компонентом "
            "«Desktop development with C++» или MinGW (g++).\n"
            f"Найденные инструменты:\n{_format_toolchain(toolchain)}"
        )
    if cuda and not toolchain.has_cuda:
        raise InstallError(
            "CUDA-сборка запрошена, но nvcc не найден. Установите CUDA Toolkit "
            "12.x или соберите CPU-версию: `--cpu`.\n"
            f"Найденные инструменты:\n{_format_toolchain(toolchain)}"
        )

    source_dir = paths.cache_dir / "omnivoice.cpp"
    if not source_dir.exists():
        if console:
            console.print("[dim]Клонирую omnivoice.cpp...[/dim]")
        _run(
            [
                git,
                "clone",
                "--recursive",
                "--depth",
                "1",
                engine_manifest()["binary"]["source"],
                str(source_dir),
            ]
        )
    elif update:
        _run([git, "pull", "--ff-only"], cwd=source_dir)
        _run(
            [git, "submodule", "update", "--init", "--recursive", "--depth", "1"],
            cwd=source_dir,
        )

    build_dir = source_dir / "build"
    if sys.platform == "win32" and toolchain.ninja:
        cache_file = build_dir / "CMakeCache.txt"
        if cache_file.exists():
            try:
                cache_text = cache_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                cache_text = ""
            if "CMAKE_GENERATOR:INTERNAL=Ninja" not in cache_text:
                # A previous configure may have selected the Visual Studio
                # generator, which cannot be switched in-place.
                if console:
                    console.print("[dim]Сбрасываю старый CMake cache (смена генератора)...[/dim]")
                shutil.rmtree(build_dir, ignore_errors=True)
    configure = [
        str(cmake),
        "-S",
        str(source_dir),
        "-B",
        str(build_dir),
        "-DCMAKE_BUILD_TYPE=Release",
    ]
    if sys.platform == "darwin":
        configure.extend(["-DGGML_METAL=ON", "-DGGML_BLAS=ON"])
    elif sys.platform == "win32":
        if cuda:
            configure.append("-DGGML_CUDA=ON")
        elif vulkan:
            configure.append("-DGGML_VULKAN=ON")
    else:
        configure.append("-DGGML_BLAS=ON")
        if cuda:
            configure.append("-DGGML_CUDA=ON")

    if cuda:
        if toolchain.cuda_root is not None:
            configure.append(f"-DCUDAToolkit_ROOT={toolchain.cuda_root}")
        if toolchain.nvcc is not None:
            configure.append(f"-DCMAKE_CUDA_COMPILER={toolchain.nvcc}")
        if _cmake_version_tuple(toolchain.cmake_version) >= (3, 24):
            configure.append("-DCMAKE_CUDA_ARCHITECTURES=native")

    if sys.platform == "win32":
        # Prefer Ninja even with MSVC: the Visual Studio generator requires
        # the CUDA Toolkit's VS integration props, which is a separate
        # component and is often missing.  Ninja only needs cl.exe + nvcc.
        if toolchain.ninja and (toolchain.has_compiler or toolchain.gpp):
            configure.extend(["-G", "Ninja"])
        elif toolchain.gpp and toolchain.make:
            configure.extend(["-G", "MinGW Makefiles"])

    build_env = toolchain.environment(include_msvc=sys.platform == "win32")
    if console:
        console.print("[dim]CMake configure...[/dim]")
        console.print(f"[dim]Использую CMake: {cmake}[/dim]")
        if cuda:
            console.print(f"[dim]CUDA: {toolchain.nvcc}[/dim]")
    _run(configure, env=build_env)
    if console:
        console.print("[dim]Сборка tts-server (это может занять несколько минут)...[/dim]")
    _run(
        [str(cmake), "--build", str(build_dir), "--config", "Release", "--parallel"],
        env=build_env,
    )

    built = _find_built_binary(source_dir)
    if built is None:
        raise InstallError(
            "Сборка завершилась, но tts-server не найден. "
            f"Проверьте {build_dir} и лог CMake."
        )
    destination = binary_path(paths)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built, destination)
    _copy_runtime_libs(build_dir, built, destination)
    if sys.platform != "win32":
        destination.chmod(0o755)
    if sys.platform == "darwin":
        subprocess.run(
            ["xattr", "-dr", "com.apple.quarantine", str(destination)],
            check=False,
            capture_output=True,
        )
    if console:
        console.print(f"[green]tts-server собран:[/green] {destination}")
    return destination


def install_binary(
    paths: AppPaths,
    *,
    offline: bool = False,
    build: bool = False,
    cuda: bool = False,
    vulkan: bool = False,
    force_cpu: bool = False,
    console: Console | None = None,
) -> Path:
    destination = binary_path(paths)
    if destination.exists() and not build:
        # Old installs copied only the executable.  The shared ggml
        # backends live next to it in the build tree; migrate them once.
        build_dir = paths.cache_dir / "omnivoice.cpp" / "build"
        if build_dir.exists():
            _copy_runtime_libs(build_dir, destination, destination)
        if console:
            console.print(f"[dim]tts-server уже есть: {destination}[/dim]")
        return destination

    assets = engine_manifest().get("binary", {}).get("assets", {})
    asset = assets.get(platform_key())
    if asset and asset.get("url") and not offline and not build:
        # Prebuilt archive support.  Kept for CI-produced binaries.
        paths.bin_dir.mkdir(parents=True, exist_ok=True)
        archive = paths.cache_dir / "downloads" / Path(asset["url"]).name
        client = httpx.Client(follow_redirects=True, timeout=120.0)
        try:
            _download_with_retry(
                client,
                asset["url"],
                archive,
                size=int(asset.get("size", 0)),
                sha256=str(asset.get("sha256", "")),
                force=True,
                console=console,
            )
        finally:
            client.close()
        if archive.suffix.lower() in {".zip"}:
            import zipfile

            with zipfile.ZipFile(archive) as zf:
                for member in zf.namelist():
                    if member.endswith("/"):
                        continue
                    name = Path(member).name
                    is_runtime = (
                        name in {binary_name(), "tts-server", "tts-server.exe"}
                        or name.lower().endswith((".dll", ".so", ".dylib"))
                        or ".so." in name
                    )
                    if not is_runtime:
                        continue
                    zf.extract(member, paths.bin_dir)
                    extracted = paths.bin_dir / member
                    target = paths.bin_dir / name
                    if extracted != target:
                        shutil.move(str(extracted), str(target))
            if not destination.exists():
                alternative = paths.bin_dir / "tts-server"
                if alternative.exists():
                    alternative.replace(destination)
        else:
            raise InstallError(f"Неизвестный формат архива: {archive}")
        destination.chmod(0o755)
        if console:
            console.print(f"[green]tts-server:[/green] {destination}")
        return destination

    toolchain = detect_toolchain()
    if not cuda and not vulkan and not force_cpu:
        if toolchain.has_cuda:
            cuda = True
        elif console and sys.platform != "darwin":
            console.print(
                "[yellow]CUDA Toolkit не найден: будет CPU-сборка.[/yellow] "
                "CPU-инференс OmniVoice.cpp очень медленный; для RTX нужен CUDA."
            )
    if cuda and not toolchain.has_cuda:
        raise InstallError(
            "Запрошена CUDA-сборка, но nvcc не найден. Установите CUDA Toolkit "
            "12.x или используйте --cpu.\n"
            f"Найденные инструменты:\n{_format_toolchain(toolchain)}"
        )
    if force_cpu and console:
        console.print(
            "[yellow]CPU-сборка OmniVoice.cpp: ожидайте несколько минут на "
            "короткую фразу. Для RTX 3060 нужен CUDA.[/yellow]"
        )
    return build_omnivoice_cpp(paths, cuda=cuda, vulkan=vulkan, console=console)


# ---------------------------------------------------------------------------
# voice / config
# ---------------------------------------------------------------------------
def reference_assets(assets_dir: str | Path | None = None) -> tuple[Path, Path] | None:
    """Return ``(audio, transcript)`` from ``assets/`` when present.

    The repository ships ``assets/ref.mp3`` and ``assets/ref.txt`` as an
    optional reference for a cloned voice.  Nothing here is required: the
    fast profile uses Piper/Kokoro and never touches these files.
    """
    roots: list[Path] = []
    if assets_dir is not None:
        roots.append(Path(assets_dir).expanduser())
    else:
        roots.extend(
            [
                Path.cwd() / "assets",
                Path(__file__).resolve().parents[2] / "assets",
            ]
        )
    for root in roots:
        text = root / "ref.txt"
        if not text.exists():
            continue
        for name in ("ref.wav", "ref.mp3", "ref.flac", "ref.m4a", "ref.ogg"):
            audio = root / name
            if audio.exists():
                return audio, text
    return None


def import_voice(
    paths: AppPaths,
    *,
    name: str = DEFAULT_VOICE,
    target: str | None = None,
    source: str = "none",
    audio: str | Path | None = None,
    text: str | Path | None = None,
    assets_dir: str | Path | None = None,
    console: Console | None = None,
) -> VoiceAsset:
    target = target or slugify(name)
    if source == "voicestudio":
        return import_from_voicestudio(name, paths, target=target)
    if source == "assets":
        found = reference_assets(assets_dir)
        if found is None:
            raise InstallError(
                "Не найдены assets/ref.mp3 и assets/ref.txt. Укажите "
                "--voice-source file --voice-audio ... --voice-text ..."
            )
        return import_from_file(name, found[0], found[1], paths, target=target)
    if source == "file":
        if audio is None or text is None:
            raise InstallError("Для --voice-source file нужны --voice-audio и --voice-text")
        text_value: str | Path = text
        if isinstance(text, str) and Path(text).expanduser().exists():
            text_value = Path(text).expanduser()
        return import_from_file(name, Path(audio), text_value, paths, target=target)
    raise InstallError(
        "--voice-source должен быть none, assets, file или voicestudio"
    )


def configure_voice(
    paths: AppPaths,
    *,
    name: str = DEFAULT_VOICE,
    target: str | None = None,
    engine: str = "omnivoice_cpp",
    language: str = "all",
) -> Path:
    target = target or slugify(name)
    config = load_config(paths=paths)
    voice = VoiceConfig(engine=engine, voice=target)  # type: ignore[arg-type]
    if language in {"ru", "all"}:
        config.tts.ru = voice
    if language in {"en", "all"}:
        config.tts.en = voice
    return save_config(config, paths=paths)


def warmup_omnivoice_cpp(
    paths: AppPaths,
    *,
    name: str = "jarvis",
    console: Console | None = None,
) -> None:
    import asyncio

    config = load_config(paths=paths)
    provider = OmniVoiceCppTTS(name, paths, config.tts.omnivoice_cpp, console=console)
    if console:
        console.print("[dim]Прогреваю OmniVoice.cpp (загрузка модели и голоса)...[/dim]")

    async def run() -> None:
        try:
            await provider.warmup()
        finally:
            await provider.close()

    asyncio.run(run())
    if console:
        console.print("[green]OmniVoice.cpp прогрет.[/green]")


def print_next_steps(console: Console, *, python_cmd: str = "uv run jarvis") -> None:
    console.print("\n[bold green]Готово.[/bold green]")
    console.print(f"Запуск:  [bold]{python_cmd} start[/bold]")
    console.print(f"Проверка: [bold]{python_cmd} doctor[/bold]")
    console.print("Скажи:   «Джарвис, открой калькулятор»\n")


def _ensure_fast_voices(paths: AppPaths, *, offline: bool, console: Console) -> None:
    """Download the small Piper/Kokoro models used by the fast profile."""
    if offline:
        console.print("[dim]Offline: Piper/Kokoro скачаются при первом синтезе.[/dim]")
        return
    try:
        model = ensure_piper_voice("ru_RU-ruslan-medium", paths.voices_dir, console=console)
        console.print(f"[green]Piper RU:[/green] {model}")
    except Exception as exc:  # noqa: BLE001 - download is best-effort
        console.print(f"[yellow]Piper не скачан:[/yellow] {exc}")
    try:
        model, voices = ensure_kokoro(paths.models_dir, console=console)
        console.print(f"[green]Kokoro EN:[/green] {model} + {voices.name}")
    except Exception as exc:  # noqa: BLE001 - download is best-effort
        console.print(f"[yellow]Kokoro не скачан:[/yellow] {exc}")


def _configure_fast_voices(paths: AppPaths) -> None:
    config = load_config(paths=paths)
    config.tts.ru = VoiceConfig(engine="piper", voice="ru_RU-ruslan-medium")
    config.tts.en = VoiceConfig(engine="kokoro", voice="bm_george")
    save_config(config, paths=paths)


def _print_clone_help(console: Console) -> None:
    console.print(
        "\n[bold]Голос по умолчанию:[/bold] Piper (RU) + Kokoro (EN) — локально, "
        "без клонирования и без VoiceStudio."
    )
    console.print(
        "[dim]Если нужен клон, положите reference-аудио и точный текст рядом: "
        "assets/ref.mp3 + assets/ref.txt, затем:[/dim]"
    )
    console.print(
        "  [bold]./install.sh --profile clone --voice-source assets[/bold]"
    )
    console.print(
        "  [dim]или: jarvis voices import --name JARVIS --from file "
        "--audio ref.wav --text ref.txt[/dim]"
    )


def _fallback_to_fast(
    paths: AppPaths,
    console: Console,
    *,
    reason: Exception | str,
    offline: bool = False,
) -> None:
    console.print(f"[yellow]Клон недоступен:[/yellow] {reason}")
    _configure_fast_voices(paths)
    _ensure_fast_voices(paths, offline=offline, console=console)
    _print_clone_help(console)
    print_next_steps(console)


def install(
    *,
    variant: str = DEFAULT_VARIANT,
    engine: str = "omnivoice-cpp",
    offline: bool = False,
    accept_license: bool = False,
    models: str | Path | None = None,
    build: bool = False,
    cuda: bool = False,
    vulkan: bool = False,
    force_cpu: bool = False,
    profile: str = "auto",
    voice_name: str = DEFAULT_VOICE,
    voice_source: str = "none",
    voice_audio: str | Path | None = None,
    voice_text: str | Path | None = None,
    assets_dir: str | Path | None = None,
    no_voice: bool = False,
    language: str = "all",
    warmup: bool = True,
    console: Console | None = None,
) -> None:
    """Full install flow used by ``scripts/install.py``.

    ``profile`` controls the TTS strategy:

    * ``auto``  — clone only when a reference voice was requested, otherwise
      fast local Piper/Kokoro;
    * ``fast``  — always Piper/Kokoro, no native build;
    * ``clone`` — OmniVoice.cpp + reference voice, falling back to fast TTS
      when the native build or the import fails.
    """
    console = console or Console()
    paths = get_paths().ensure()
    if language == "both":
        language = "all"
    profile = profile.strip().lower()
    if profile not in {"auto", "fast", "clone"}:
        raise InstallError("--profile должен быть auto, fast или clone")
    if no_voice:
        voice_source = "none"
    voice_source = voice_source.strip().lower()
    if voice_source not in {"none", "assets", "file", "voicestudio"}:
        raise InstallError(
            "--voice-source должен быть none, assets, file или voicestudio"
        )
    if voice_audio and voice_text and voice_source == "none":
        voice_source = "file"

    if engine != "omnivoice-cpp":
        raise InstallError(
            "Сейчас установщик поддерживает только --engine omnivoice-cpp. "
            "Piper/Kokoro — это профиль fast и команда `jarvis voices install`."
        )

    init_config(paths)
    clone_requested = (
        voice_source != "none" or build or cuda or vulkan or force_cpu
    )
    if profile == "auto":
        profile = "clone" if clone_requested else "fast"
    if profile == "clone" and voice_source == "none":
        if reference_assets(assets_dir) is not None:
            voice_source = "assets"
            console.print("[dim]Найден assets/ref.* — использую его как референс.[/dim]")
        else:
            raise InstallError(
                "Профиль clone требует референс-голос. Укажите "
                "--voice-source assets (assets/ref.mp3 + ref.txt), "
                "--voice-source file --voice-audio ... --voice-text ..., "
                "либо --voice-source voicestudio. Либо используйте "
                "--profile fast для Piper/Kokoro."
            )

    if profile == "fast":
        _configure_fast_voices(paths)
        _ensure_fast_voices(paths, offline=offline, console=console)
        _print_clone_help(console)
        print_next_steps(console)
        return

    if voice_source == "assets" and reference_assets(assets_dir) is None:
        raise InstallError(
            "Не найдены assets/ref.mp3 и assets/ref.txt. Положите файлы в "
            "assets/ или используйте --voice-source file --voice-audio ... "
            "--voice-text ..."
        )
    if voice_source == "file" and (voice_audio is None or voice_text is None):
        raise InstallError(
            "Для --voice-source file нужны --voice-audio и --voice-text"
        )

    toolchain = detect_toolchain()
    missing_compiler = (
        sys.platform == "win32"
        and not toolchain.has_compiler
        and not toolchain.gpp
    )
    asset = engine_manifest().get("binary", {}).get("assets", {}).get(platform_key())
    can_use_prebuilt = bool(asset and asset.get("url") and not build and not offline)
    if (
        not binary_path(paths).exists()
        and not can_use_prebuilt
        and (toolchain.cmake is None or missing_compiler)
    ):
        _fallback_to_fast(
            paths,
            console,
            reason="не найден CMake или компилятор C++",
            offline=offline,
        )
        return

    base: Path | None = None
    codec: Path | None = None
    try:
        base, codec = fetch_omnivoice_models(
            paths,
            variant=variant,
            directory=models,
            offline=offline,
            accept_license=accept_license,
            console=console,
        )
        console.print(f"[green]Модель:[/green] {base}")
        console.print(f"[green]Codec:[/green] {codec}")
    except (InstallError, OSError) as exc:
        _fallback_to_fast(paths, console, reason=exc, offline=offline)
        return

    try:
        binary = install_binary(
            paths,
            offline=offline,
            build=build,
            cuda=cuda,
            vulkan=vulkan,
            force_cpu=force_cpu,
            console=console,
        )
        console.print(f"[green]Сервер:[/green] {binary}")
    except (InstallError, OSError) as exc:
        _fallback_to_fast(paths, console, reason=exc, offline=offline)
        return

    config = load_config(paths=paths)
    if base is not None:
        config.tts.omnivoice_cpp.model = str(base)
    if codec is not None:
        config.tts.omnivoice_cpp.codec = str(codec)
    if force_cpu:
        # CPU inference is ~80x slower than real time; do not let a short
        # phrase trip the normal request timeout.
        config.tts.omnivoice_cpp.request_timeout = 900.0
    save_config(config, paths=paths)

    try:
        asset = import_voice(
            paths,
            name=voice_name,
            source=voice_source,
            audio=voice_audio,
            text=voice_text,
            assets_dir=assets_dir,
            console=console,
        )
    except (VoiceImportError, InstallError) as exc:
        _fallback_to_fast(paths, console, reason=exc, offline=offline)
        return

    console.print(f"[green]Голос:[/green] {asset.name} ({asset.directory})")
    configure_voice(
        paths,
        name=voice_name,
        target=asset.name,
        engine="omnivoice_cpp",
        language=language,
    )

    if warmup:
        try:
            warmup_omnivoice_cpp(paths, name=asset.name, console=console)
        except Exception as exc:  # noqa: BLE001 - warmup is best-effort
            console.print(f"[yellow]Прогрев не удался:[/yellow] {exc}")

    print_next_steps(console)
