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
def _run(cmd: list[str], *, cwd: Path | None = None) -> None:
    result = subprocess.run(cmd, cwd=str(cwd) if cwd else None)
    if result.returncode != 0:
        raise InstallError(f"Команда завершилась с кодом {result.returncode}: {' '.join(cmd)}")


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
    return None


def build_omnivoice_cpp(
    paths: AppPaths,
    *,
    cuda: bool = False,
    vulkan: bool = False,
    update: bool = False,
    console: Console | None = None,
) -> Path:
    """Clone and build omnivoice.cpp, then install ``tts-server``."""
    git = shutil.which("git")
    cmake = shutil.which("cmake")
    if not git:
        raise InstallError("Для сборки нужен git")
    if not cmake:
        raise InstallError(
            "Для сборки нужен CMake 3.14+. Установите его: "
            "Windows — https://cmake.org/download/ (или `winget install Kitware.CMake`), "
            "macOS — `brew install cmake`."
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
    configure = [
        cmake,
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

    if sys.platform == "win32" and cuda and not shutil.which("cl"):
        raise InstallError(
            "CUDA-сборка на Windows требует MSVC (Visual Studio Build Tools). "
            "Запустите установщик из 'x64 Native Tools Command Prompt' или "
            "установите Build Tools с компонентом C++."
        )

    if sys.platform == "win32" and not shutil.which("cl"):
        # No MSVC environment: use a MinGW/Ninja toolchain if one is present.
        if shutil.which("g++") and shutil.which("ninja"):
            configure.extend(["-G", "Ninja"])
        elif shutil.which("g++") and (
            shutil.which("mingw32-make") or shutil.which("make")
        ):
            configure.extend(["-G", "MinGW Makefiles"])

    if console:
        console.print("[dim]CMake configure...[/dim]")
    _run(configure)
    if console:
        console.print("[dim]Сборка tts-server (это может занять несколько минут)...[/dim]")
    _run([cmake, "--build", str(build_dir), "--config", "Release", "--parallel"])

    built = _find_built_binary(source_dir)
    if built is None:
        raise InstallError(
            "Сборка завершилась, но tts-server не найден. "
            f"Проверьте {build_dir} и лог CMake."
        )
    destination = binary_path(paths)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built, destination)
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
        if console:
            console.print(f"[dim]tts-server уже есть: {destination}[/dim]")
        return destination

    assets = engine_manifest().get("binary", {}).get("assets", {})
    asset = assets.get(platform_key())
    if asset and asset.get("url") and not offline:
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
                    if Path(member).name in {binary_name(), "tts-server", "tts-server.exe"}:
                        zf.extract(member, paths.bin_dir)
                        extracted = paths.bin_dir / member
                        if extracted != destination:
                            shutil.move(str(extracted), destination)
                        break
        else:
            raise InstallError(f"Неизвестный формат архива: {archive}")
        destination.chmod(0o755)
        if console:
            console.print(f"[green]tts-server:[/green] {destination}")
        return destination

    if not cuda and not vulkan and not force_cpu and not build:
        has_nvidia = bool(shutil.which("nvidia-smi"))
        has_nvcc = bool(shutil.which("nvcc"))
        if has_nvidia and has_nvcc:
            cuda = True
        elif has_nvidia:
            raise InstallError(
                "Найдена NVIDIA GPU, но nvcc не установлен. CPU-сборка "
                "OmniVoice.cpp очень медленная (RTF ~80 на тестовой машине). "
                "Установите CUDA Toolkit 12.8 и повторите с --cuda, либо "
                "явно согласитесь на CPU: --cpu."
            )
        elif console and sys.platform != "darwin":
            console.print(
                "[yellow]NVIDIA GPU не найдена: будет CPU-сборка.[/yellow] "
                "Ожидайте несколько минут на короткую фразу."
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
def import_voice(
    paths: AppPaths,
    *,
    name: str = DEFAULT_VOICE,
    target: str | None = None,
    source: str = "voicestudio",
    audio: str | Path | None = None,
    text: str | Path | None = None,
    console: Console | None = None,
) -> VoiceAsset:
    target = target or slugify(name)
    if source == "voicestudio":
        return import_from_voicestudio(name, paths, target=target)
    if source == "file":
        if audio is None or text is None:
            raise InstallError("Для --voice-source file нужны --voice-audio и --voice-text")
        text_value: str | Path = text
        if isinstance(text, str) and Path(text).expanduser().exists():
            text_value = Path(text).expanduser()
        return import_from_file(name, Path(audio), text_value, paths, target=target)
    raise InstallError("--voice-source должен быть voicestudio или file")


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

    async def run() -> None:
        try:
            await provider.warmup()
        finally:
            await provider.close()

    asyncio.run(run())


def print_next_steps(console: Console, *, python_cmd: str = "uv run jarvis") -> None:
    console.print("\n[bold green]Готово.[/bold green]")
    console.print(f"Запуск:  [bold]{python_cmd} start[/bold]")
    console.print(f"Проверка: [bold]{python_cmd} doctor[/bold]")
    console.print("Скажи:   «Джарвис, открой калькулятор»\n")


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
    voice_name: str = DEFAULT_VOICE,
    voice_source: str = "voicestudio",
    voice_audio: str | Path | None = None,
    voice_text: str | Path | None = None,
    no_voice: bool = False,
    language: str = "all",
    warmup: bool = True,
    console: Console | None = None,
) -> None:
    """Full install flow used by ``scripts/install.py``."""
    console = console or Console()
    paths = get_paths().ensure()
    if language == "both":
        language = "all"

    if engine != "omnivoice-cpp":
        raise InstallError(
            "Сейчас установщик поддерживает только --engine omnivoice-cpp. "
            "Piper/Kokoro ставятся через `jarvis voices install`."
        )

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

    config, _ = init_config(paths)
    config = load_config(paths=paths)
    config.tts.omnivoice_cpp.model = str(base)
    config.tts.omnivoice_cpp.codec = str(codec)
    if force_cpu:
        # CPU inference is ~80x slower than real time; do not let a short
        # phrase trip the normal request timeout.
        config.tts.omnivoice_cpp.request_timeout = 900.0
    save_config(config, paths=paths)

    voice_asset_name: str | None = None
    if voice_audio and voice_text and voice_source == "voicestudio":
        voice_source = "file"
    if not no_voice:
        try:
            asset = import_voice(
                paths,
                name=voice_name,
                source=voice_source,
                audio=voice_audio,
                text=voice_text,
                console=console,
            )
            voice_asset_name = asset.name
            console.print(f"[green]Голос:[/green] {asset.name} ({asset.directory})")
            configure_voice(
                paths,
                name=voice_name,
                target=asset.name,
                engine="omnivoice_cpp",
                language=language,
            )
        except VoiceImportError as exc:
            console.print(f"[yellow]Голос не импортирован:[/yellow] {exc}")
            console.print(
                "Импортируйте позже: [bold]jarvis voices import --name JARVIS "
                "--from voicestudio[/bold]"
            )

    if warmup and voice_asset_name:
        try:
            warmup_omnivoice_cpp(paths, name=voice_asset_name, console=console)
        except Exception as exc:  # noqa: BLE001 - warmup is best-effort
            console.print(f"[yellow]Прогрев не удался:[/yellow] {exc}")

    print_next_steps(console)
