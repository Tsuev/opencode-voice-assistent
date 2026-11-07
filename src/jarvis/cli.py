"""Jarvis command-line interface."""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .config import (
    JarvisConfig,
    VoiceConfig,
    ensure_api_token,
    init_config,
    load_config,
    save_config,
)
from .doctor import checks_as_json, run_checks
from .events import EventType, Subscription
from .logging_setup import setup_logging
from .opencode.agent import install_agent
from .opencode.client import list_models
from .paths import AppPaths, get_paths
from .runtime import Runtime
from .tts.catalog import ALL_VOICES
from .tts.downloads import ensure_kokoro, ensure_piper_voice

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Локальный голосовой ассистент для OpenCode.",
)
console = Console()

# Legacy Windows consoles are often cp1251; rich's check marks and arrows then
# raise UnicodeEncodeError.  Replace unencodable characters instead of crashing.
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(errors="replace")


@app.callback()
def main(
    ctx: typer.Context,
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Путь к config.toml (по умолчанию — системный каталог Jarvis)."
    ),
) -> None:
    ctx.obj = {"config_path": config}


def _load(ctx: typer.Context) -> tuple[JarvisConfig, AppPaths]:
    paths = get_paths().ensure()
    config_path = (ctx.obj or {}).get("config_path")
    return load_config(config_path, paths), paths


# ---------------------------------------------------------------------------
# init / doctor
# ---------------------------------------------------------------------------
@app.command()
def init(
    project: bool = typer.Option(
        False, "--project", help="Поставить агента в ./.opencode/agent вместо глобального."
    ),
    force: bool = typer.Option(False, "--force", help="Перезаписать конфиг и агента."),
    model: str | None = typer.Option(
        None, "--model", "-m", help="Зафиксировать модель OpenCode (provider/model)."
    ),
    lang: str | None = typer.Option(
        None, "--lang", "-l", help="Язык: ru или en (по умолчанию ru)."
    ),
) -> None:
    """Создать конфиг, токен API и установить агента OpenCode."""
    if lang is not None and lang not in {"ru", "en"}:
        raise typer.BadParameter("lang должен быть ru или en")
    paths = get_paths().ensure()
    config, config_path = init_config(paths, force=force)
    if lang is not None:
        config.language = lang  # type: ignore[assignment]
        save_config(config, config_path, paths)
    if model:
        config.opencode.model = model
        save_config(config, config_path, paths)
    agent_path, written = install_agent(
        language=config.language,
        project=Path.cwd() if project else None,
        force=force or lang is not None,
    )

    console.print("[bold green]Jarvis инициализирован[/bold green]")
    console.print(f"  конфиг:  {config_path}")
    console.print(f"  данные:  {paths.data_dir}")
    console.print(f"  токен:   {paths.token_file}")
    console.print(f"  язык:    {config.language}")
    console.print(
        f"  агент:   {agent_path} "
        + ("[green](создан)[/green]" if written else "[dim](уже существует)[/dim]")
    )
    console.print()
    console.print("[dim]Дальше:[/dim]")
    console.print("  jarvis doctor")
    console.print("  jarvis voices install")
    console.print('  jarvis say "Системы онлайн"')
    if not config.opencode.model:
        console.print(
            "[yellow]Совет:[/yellow] зафиксируйте opencode.model — модель по умолчанию "
            "может быть недоступна в вашем регионе."
        )


@app.command("language")
def set_language(
    ctx: typer.Context,
    code: str = typer.Argument(..., help="ru или en"),
) -> None:
    """Зафиксировать язык STT, TTS и агента."""
    if code not in {"ru", "en"}:
        raise typer.BadParameter("Язык должен быть ru или en")
    config, paths = _load(ctx)
    config.language = code  # type: ignore[assignment]
    config_path = (ctx.obj or {}).get("config_path")
    save_config(config, config_path, paths)
    agent_path, written = install_agent(language=code, force=True)
    console.print(f"[green]Язык:[/green] {code}")
    console.print(f"[green]Агент:[/green] {agent_path}")
    if not written:
        console.print("[yellow]Агент не обновлён[/yellow]")
    console.print("[dim]Перезапустите `jarvis start`, чтобы применить.[/dim]")


@app.command()
def doctor(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Вывести результат в JSON."),
) -> None:
    """Проверить окружение, аудио, модели и OpenCode."""
    config, paths = _load(ctx)
    checks = run_checks(config, paths)
    if json_output:
        console.print_json(checks_as_json(checks))
        return

    table = Table(title="Jarvis doctor", show_lines=False, header_style="bold")
    table.add_column("Проверка", style="bold")
    table.add_column("Статус", justify="center")
    table.add_column("Детали", overflow="fold")
    table.add_column("Как исправить", overflow="fold", style="dim")

    icons = {"ok": "[green]✓[/green]", "warn": "[yellow]![/yellow]", "fail": "[red]✗[/red]"}
    for check in checks:
        table.add_row(check.name, icons[check.status], check.detail, check.fix or "")

    console.print(table)
    failures = [check for check in checks if check.status == "fail"]
    warnings = [check for check in checks if check.status == "warn"]
    console.print(
        f"\n[green]{len(checks) - len(failures) - len(warnings)} ок[/green], "
        f"[yellow]{len(warnings)} предупреждений[/yellow], "
        f"[red]{len(failures)} ошибок[/red]"
    )
    if failures:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# speech
# ---------------------------------------------------------------------------
@app.command()
def say(
    ctx: typer.Context,
    text: str = typer.Argument(..., help="Текст для озвучивания."),
    voice: str = typer.Option("auto", "--voice", "-v", help="auto | ru | en"),
    speed: float | None = typer.Option(None, "--speed", help="Множитель скорости."),
    out: Path | None = typer.Option(None, "--out", help="Сохранить WAV вместо воспроизведения."),
    no_play: bool = typer.Option(False, "--no-play", help="Не включать динамики."),
) -> None:
    """Синтезировать и воспроизвести фразу локальным голосом."""
    config, paths = _load(ctx)
    setup_logging(config.log_level, paths.log_file, console=console)
    language = config.language if voice == "auto" else voice
    if language not in (None, "ru", "en"):
        raise typer.BadParameter("voice должен быть auto, ru или en")

    async def run() -> None:
        runtime = Runtime(config, paths, console=console)
        await runtime.start()
        try:
            if out is not None:
                import soundfile as sf

                audio = await runtime.tts.synthesize(text, language=language, speed=speed)
                out.parent.mkdir(parents=True, exist_ok=True)
                sf.write(out, audio.samples, audio.sample_rate)
                console.print(f"[green]Сохранено:[/green] {out} ({audio.duration:.1f} c)")
            elif no_play:
                audio = await runtime.tts.synthesize(text, language=language, speed=speed)
                console.print(
                    f"[green]Синтез ок:[/green] {audio.duration:.1f} c, {audio.sample_rate} Гц"
                )
            else:
                started = time.perf_counter()
                await runtime.tts.speak(text, language=language, speed=speed)
                console.print(f"[dim]готово за {time.perf_counter() - started:.2f} c[/dim]")
        finally:
            await runtime.close()

    try:
        asyncio.run(run())
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


@app.command()
def start(
    ctx: typer.Context,
    no_speak: bool = typer.Option(False, "--no-speak", help="Не озвучивать ответы."),
    no_beep: bool = typer.Option(False, "--no-beep", help="Не пищать при активации."),
    engine: str | None = typer.Option(
        None, "--engine", help="vad-whisper | openwakeword | porcupine"
    ),
    wav: Path | None = typer.Option(
        None, "--wav", help="Прогнать WAV-файл вместо микрофона (отладка/CI)."
    ),
) -> None:
    """Запустить always-on голосового ассистента."""
    config, paths = _load(ctx)
    setup_logging(config.log_level, paths.log_file, console=console)
    if engine is not None:
        config.wake.engine = engine  # type: ignore[assignment]

    async def run() -> None:
        from .audio.input import WavSource
        from .stt.whisper import WhisperSTT
        from .voice import VoiceLoop
        from .wake.base import create_detector

        runtime = Runtime(config, paths, console=console)
        await runtime.start()
        detector = create_detector(
            config.wake,
            console=console,
            cache_dir=paths.cache_dir / "whisper",
            stt_config=config.stt,
            vad_config=config.vad,
        )
        stt = WhisperSTT(
            config.stt,
            cache_dir=paths.cache_dir / "whisper",
            language=config.language,
        )

        console.print("[dim]Прогреваю модели (STT/TTS)…[/dim]")
        warmups: list = []
        if config.stt.warmup:
            warmups.append(asyncio.to_thread(stt.warmup))
        wake_warmup = getattr(detector, "warmup", None)
        if callable(wake_warmup):
            warmups.append(asyncio.to_thread(wake_warmup))
        if not no_speak:
            warmups.append(runtime.tts.warmup(config.language))
        for result in await asyncio.gather(*warmups, return_exceptions=True):
            if isinstance(result, Exception):
                console.print(f"[yellow]Прогрев:[/yellow] {result}")

        source = WavSource(wav, block_ms=config.audio.block_ms) if wav is not None else None
        voice = VoiceLoop(
            runtime,
            detector,
            stt,
            config,
            console=console,
            speak=not no_speak,
            beep=not no_beep,
            microphone=source,
        )
        console.print(
            Panel(
                f"Wake word: [bold]{config.wake.phrase}[/bold] "
                f"({detector.name}, {getattr(detector, 'model_path', '—')})\n"
                f"STT:       {config.stt.model} ({config.language})\n"
                f"RU голос:  {config.tts.ru.voice}\n"
                f"EN голос:  {config.tts.en.voice}\n"
                f"Модель:    {config.opencode.model or 'по умолчанию OpenCode'}\n"
                + (f"Источник:  [yellow]WAV {wav}[/yellow]\n" if wav else "")
                + "\nСлушаю. Ctrl+C — остановить.\n"
                "[dim]Первая транскрипция может скачать модель Whisper.[/dim]",
                title="Jarvis",
            )
        )
        subscription = runtime.bus.subscribe()
        renderer = asyncio.create_task(_render_events(subscription, console))
        try:
            await voice.run()
        finally:
            voice.stop()
            with contextlib.suppress(Exception):
                await subscription.aclose()
            renderer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renderer
            await runtime.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        console.print("\n[dim]Остановлено.[/dim]")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


async def _render_events(subscription: Subscription, console: Console) -> None:
    try:
        async for event in subscription:
            if event.type == EventType.ASSISTANT_DELTA:
                console.print(event.data.get("text", ""), end="", markup=False, highlight=False)
            elif event.type == EventType.TOOL:
                console.print(
                    f"\n[dim]⚙ {event.data.get('name')} · {event.data.get('status')}[/dim]"
                )
            elif event.type == EventType.ERROR:
                console.print(f"\n[red]{event.data.get('message')}[/red]")
            elif event.type == EventType.WAKE:
                console.print(
                    f"\n[magenta]◉ wake: {event.data.get('phrase')} "
                    f"({float(event.data.get('score', 0)):.2f})[/magenta]"
                )
            elif event.type == EventType.TRANSCRIPT:
                console.print(f"[bold]Вы:[/bold] {event.data.get('text')}")
            elif event.type == EventType.SPEECH_START:
                console.print(f"\n[cyan]🔊 {event.data.get('text', '')}[/cyan]")
    except asyncio.CancelledError:
        return


@app.command()
def ask(
    ctx: typer.Context,
    text: str = typer.Argument(..., help="Запрос к OpenCode."),
    model: str | None = typer.Option(None, "--model", "-m", help="provider/model."),
    agent: str | None = typer.Option(None, "--agent", help="Агент OpenCode ('' — без агента)."),
    directory: str | None = typer.Option(None, "--directory", "-d", help="Рабочая директория."),
    attach: bool = typer.Option(False, "--attach", help="Использовать фоновый сервис OpenCode."),
    speak: bool = typer.Option(False, "--speak", help="Озвучить финальный ответ."),
) -> None:
    """Отправить один запрос в OpenCode и показать стрим ответа."""
    config, paths = _load(ctx)
    setup_logging(config.log_level, paths.log_file, console=console)
    if model is not None:
        config.opencode.model = model
    if agent is not None:
        config.opencode.agent = agent
    if directory is not None:
        config.opencode.directory = directory
    if attach:
        config.opencode.mode = "attach"

    async def run() -> str:
        runtime = Runtime(config, paths, console=console)
        await runtime.start()
        subscription = runtime.bus.subscribe()
        renderer = asyncio.create_task(_render_events(subscription, console))
        try:
            return await runtime.ask(text, speak=speak)
        finally:
            with contextlib.suppress(Exception):
                await subscription.aclose()
            renderer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renderer
            await runtime.close()

    try:
        reply = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001
        console.print(f"\n[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print()
    if not reply:
        console.print("[yellow]Пустой ответ[/yellow]")
        raise typer.Exit(code=1)
    console.print(f"[dim]символов: {len(reply)}[/dim]")


# ---------------------------------------------------------------------------
# models / voices
# ---------------------------------------------------------------------------
@app.command()
def models() -> None:
    """Показать модели, доступные OpenCode."""
    items = list_models()
    if not items:
        console.print("[yellow]OpenCode не вернул модели[/yellow]")
        raise typer.Exit(code=1)
    table = Table(title="Модели OpenCode", header_style="bold")
    table.add_column("provider/model")
    for item in items:
        table.add_row(item)
    console.print(table)


def _normalize_engine(engine: str | None) -> str | None:
    if engine is None:
        return None
    return engine.strip().lower().replace("-", "_")


@app.command()
def voices(
    ctx: typer.Context,
    action: str = typer.Argument(
        "list", help="list | install | import | use | warmup"
    ),
    language: str | None = typer.Option(None, "--lang", help="ru | en | all"),
    engine: str | None = typer.Option(
        None, "--engine", help="piper | kokoro | omnivoice-cpp | moss"
    ),
    name: str | None = typer.Option(None, "--name", help="Имя голоса, например JARVIS"),
    source: str = typer.Option(
        "file", "--from", help="file | assets | voicestudio"
    ),
    audio: Path | None = typer.Option(
        None, "--audio", help="Референс-аудио для --from file"
    ),
    text: str | None = typer.Option(None, "--text", help="Текст референса или путь к .txt"),
    target_name: str | None = typer.Option(None, "--target", help="Имя папки голоса"),
) -> None:
    """Список голосов, импорт клона, выбор движка и прогрев модели."""
    from .tts.voice_import import (
        VoiceImportError,
        import_from_file,
        import_from_voicestudio,
        list_assets,
        load_asset,
        slugify,
    )

    config, paths = _load(ctx)
    engine = _normalize_engine(engine)
    action = action.strip().lower()
    if language == "both":
        language = "all"

    if action == "list":
        assets = {asset.name: asset for asset in list_assets(paths)}
        table = Table(title="Локальные голоса", header_style="bold")
        table.add_column("Движок")
        table.add_column("Голос")
        table.add_column("Язык")
        table.add_column("Тембр")
        table.add_column("Готов")
        table.add_column("Описание", overflow="fold")
        for voice in ALL_VOICES:
            if language and language != "all" and language not in voice.language.split("+"):
                continue
            if engine and voice.engine != engine:
                continue
            if voice.engine == "piper":
                ready = (paths.voices_dir / f"{voice.name}.onnx").exists()
            elif voice.engine == "omnivoice_cpp":
                ready = voice.name in assets
            else:
                ready = (paths.models_dir / "kokoro-v1.0.onnx").exists()
            description = voice.description
            if voice.engine == "omnivoice_cpp" and voice.name in assets:
                description = f"{description}; импортирован"
            table.add_row(
                voice.engine,
                voice.name,
                voice.language,
                voice.gender,
                "[green]да[/green]" if ready else "[dim]—[/dim]",
                description,
            )
        known = {voice.name for voice in ALL_VOICES}
        for asset in assets.values():
            if asset.name in known:
                continue
            table.add_row(
                "clone",
                asset.name,
                "ru+en",
                "clone",
                "[green]да[/green]",
                f"импортирован ({asset.profile.get('source', 'file')})",
            )
        console.print(table)
        if not assets:
            console.print(
                "[dim]Клон ещё не импортирован: "
                "`jarvis voices import --name JARVIS --from assets` "
                "(или --from file --audio ... --text ...)[/dim]"
            )
        return

    if action == "install":
        target = language or "all"
        if engine in {None, "piper"} and target in {"ru", "all"}:
            piper_voice = (
                config.tts.ru.voice
                if config.tts.ru.engine == "piper"
                else "ru_RU-ruslan-medium"
            )
            ensure_piper_voice(piper_voice, paths.voices_dir, console=console)
        if engine in {None, "kokoro"} and target in {"en", "all"}:
            ensure_kokoro(paths.models_dir, console=console)
        if engine == "omnivoice_cpp":
            from .tts.omnivoice_cpp import OmniVoiceCppTTS

            probe = OmniVoiceCppTTS("jarvis", paths, config.tts.omnivoice_cpp)
            ready, reason = probe.model_ready()
            if ready:
                console.print(f"[green]OmniVoice.cpp готов:[/green] {reason}")
                console.print(
                    "Импортируйте голос: [bold]jarvis voices import "
                    "--name JARVIS --from assets[/bold] "
                    "(или --from file --audio ... --text ...)"
                )
            else:
                console.print(f"[yellow]OmniVoice.cpp не готов:[/yellow] {reason}")
                console.print(
                    "Скачайте веса и бинарник:\n"
                    "  uv run python scripts/fetch_models.py --engine omnivoice-cpp "
                    "--model q8 --accept-license\n"
                    "  ./install.sh --build-from-source   # если нет prebuilt"
                )
            return
        if engine == "moss":
            console.print(
                "[yellow]MOSS пока не подключён.[/yellow] "
                "Он появится как опциональный ONNX-движок позже."
            )
            return
        if engine not in {None, "piper", "kokoro"}:
            raise typer.BadParameter("engine должен быть piper, kokoro или omnivoice-cpp")
        console.print("[green]Готово.[/green] Запустите `jarvis voices` для проверки.")
        return

    if action == "import":
        voice_name = (name or "JARVIS").strip()
        target = target_name or slugify(voice_name)
        try:
            if source == "voicestudio":
                asset = import_from_voicestudio(voice_name, paths, target=target)
            elif source == "assets":
                from .installer import reference_assets

                found = reference_assets()
                if found is None:
                    raise typer.BadParameter(
                        "Не найдены assets/ref.mp3 и assets/ref.txt"
                    )
                asset = import_from_file(
                    voice_name, found[0], found[1], paths, target=target
                )
            elif source == "file":
                if audio is None or text is None:
                    raise typer.BadParameter("Для --from file нужны --audio и --text")
                text_value: str | Path = text
                if text and Path(text).expanduser().exists():
                    text_value = Path(text).expanduser()
                asset = import_from_file(
                    voice_name,
                    audio,
                    text_value,
                    paths,
                    target=target,
                )
            else:
                raise typer.BadParameter(
                    "--from должен быть file, assets или voicestudio"
                )
        except VoiceImportError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(f"[green]Импортирован:[/green] {asset.name}")
        console.print(f"  папка: {asset.directory}")
        console.print(f"  аудио: {asset.reference_audio}")
        console.print(f"  текст: {asset.reference_text[:80]}...")
        return

    if action == "use":
        voice_name = (name or "JARVIS").strip()
        target = target_name or slugify(voice_name)
        chosen_engine = engine or "omnivoice_cpp"
        if chosen_engine not in {"piper", "kokoro", "say", "omnivoice_cpp"}:
            raise typer.BadParameter(
                "пока доступны piper, kokoro, say и omnivoice-cpp"
            )
        if chosen_engine == "omnivoice_cpp":
            try:
                load_asset(target, paths)
            except VoiceImportError as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=1) from exc
        scope = (language or "all").lower()
        if scope == "both":
            scope = "all"
        if scope not in {"ru", "en", "all"}:
            raise typer.BadParameter("--lang должен быть ru, en, all или both")
        voice_config = VoiceConfig(engine=chosen_engine, voice=target)  # type: ignore[arg-type]
        if scope in {"ru", "all"}:
            config.tts.ru = voice_config
        if scope in {"en", "all"}:
            config.tts.en = voice_config
        path = save_config(config, paths=paths)
        console.print(
            f"[green]Голос «{target}» выбран для {scope}.[/green] Конфиг: {path}"
        )
        if chosen_engine == "omnivoice_cpp":
            console.print(
                f"Прогрейте модель: [bold]jarvis voices warmup --name {voice_name}[/bold]"
            )
        return

    if action == "warmup":
        voice_name = (name or "JARVIS").strip()
        target = target_name or slugify(voice_name)
        from .tts.omnivoice_cpp import OmniVoiceCppError, OmniVoiceCppTTS

        provider = OmniVoiceCppTTS(
            target, paths, config.tts.omnivoice_cpp, console=console
        )

        async def run() -> None:
            started = time.perf_counter()
            try:
                await provider.warmup()
            except OmniVoiceCppError as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=1) from exc
            finally:
                await provider.close()
            console.print(
                f"[green]OmniVoice.cpp прогрет за "
                f"{time.perf_counter() - started:.1f} c[/green]"
            )

        asyncio.run(run())
        return

    raise typer.BadParameter(
        "action должен быть list, install, import, use или warmup"
    )


# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------
@app.command("wake-test")
def wake_test(
    ctx: typer.Context,
    seconds: float = typer.Option(20.0, "--seconds", "-s", help="0 — до Ctrl+C."),
    engine: str | None = typer.Option(
        None, "--engine", help="vad-whisper | openwakeword | porcupine"
    ),
    beep: bool = typer.Option(False, "--beep", help="Пищать при детекции."),
    debug: bool = typer.Option(
        False, "--debug", help="Печатать каждый распознанный wake-сегмент."
    ),
) -> None:
    """Проверить wake word на живом микрофоне."""
    config, paths = _load(ctx)
    setup_logging(config.log_level, paths.log_file, console=console)
    if engine is not None:
        config.wake.engine = engine  # type: ignore[assignment]
    if debug:
        config.wake.debug = True

    from .audio.input import Microphone
    from .wake.base import create_detector

    detector = create_detector(
        config.wake,
        console=console,
        cache_dir=paths.cache_dir / "whisper",
        stt_config=config.stt,
        vad_config=config.vad,
    )
    microphone = Microphone(
        config.audio.input_device,
        sample_rate=16000,
        block_ms=config.audio.block_ms,
    )
    phrase = getattr(detector, "phrase", config.wake.phrase)
    model_path = getattr(detector, "model_path", "—")
    console.print(
        Panel(
            f"Движок: [bold]{detector.name}[/bold]\n"
            f"Фраза:  [bold]{phrase}[/bold]\n"
            f"Порог:  {config.wake.threshold}\n"
            f"Модель: {model_path}\n\n"
            "Скажите фразу. Ctrl+C — остановить.",
            title="Wake word test",
        )
    )

    detections = 0
    peak = 0.0
    signal_peak = 0.0
    started = time.monotonic()

    def render() -> Panel:
        elapsed = time.monotonic() - started
        return Panel(
            f"score: [bold]{detector.last_score:.3f}[/bold]   "
            f"peak: {peak:.3f}   detections: [green]{detections}[/green]\n"
            f"последний текст: {detector.last_text or '—'}\n"
            f"прошло: {elapsed:.1f} c",
            title="Слушаю…",
        )

    try:
        with Live(render(), console=console, refresh_per_second=10, transient=False) as live:
            for frame in microphone.frames():
                if seconds and time.monotonic() - started >= seconds:
                    break
                hit = detector.process(frame)
                peak = max(peak, detector.last_score)
                signal_peak = max(
                    signal_peak,
                    float(abs(frame).max()) if len(frame) else 0.0,
                )
                if hit:
                    detections += 1
                    live.console.print(
                        f"[green]✓ {phrase}[/green] "
                        f"(score {detector.last_score:.3f}, {time.monotonic() - started:.1f} c)"
                    )
                    if beep:
                        _beep()
                live.update(render())
    except KeyboardInterrupt:
        console.print("\n[dim]Остановлено.[/dim]")

    console.print(
        f"\nИтог: [bold]{detections}[/bold] детекций, peak score {peak:.3f}. "
        f"Порог {config.wake.threshold}."
    )
    if signal_peak == 0:
        console.print(
            "[red]Микрофон возвращает тишину.[/red] Разрешите доступ к микрофону "
            "в настройках ОС (macOS: Privacy & Security → Microphone)."
        )
    elif detections == 0:
        console.print(
            "[yellow]Совет:[/yellow] проверьте микрофон и произношение. "
            "Для фразы «Джарвис» используйте `--engine vad-whisper` "
            "(фраза wake.phrase = \"джарвис\")."
        )


def _beep() -> None:
    try:
        import numpy as np
        import sounddevice as sd

        samples = np.sin(2 * np.pi * 880 * np.arange(0, 0.12, 1 / 16000)).astype("float32")
        sd.play(samples * 0.15, 16000)
    except Exception:  # noqa: BLE001
        pass


@app.command("stt-test")
def stt_test(
    ctx: typer.Context,
    seconds: float = typer.Option(5.0, "--seconds", "-s", help="Длительность записи."),
) -> None:
    """Записать фразу и распознать её локальным Whisper."""
    config, paths = _load(ctx)
    setup_logging(config.log_level, paths.log_file, console=console)

    from .audio.input import record_seconds_checked
    from .stt.whisper import WhisperSTT

    console.print(f"[bold]Говорите[/bold] ({seconds:.0f} c)…")
    try:
        samples = record_seconds_checked(
            seconds, sample_rate=16000, device=config.audio.input_device
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    import numpy as np

    rms = float(np.sqrt(np.mean(np.square(samples))))
    if rms <= 1e-7:
        console.print(
            "[red]Микрофон возвращает тишину.[/red] Разрешите доступ к микрофону "
            "в настройках ОС и попробуйте снова."
        )
        raise typer.Exit(code=1)
    console.print("[dim]Распознаю…[/dim]")
    stt = WhisperSTT(
        config.stt,
        cache_dir=paths.cache_dir / "whisper",
        language=config.language,
    )

    async def run() -> str:
        return await stt.transcribe(samples, 16000)

    try:
        text = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Текст:[/green] {text or '[пусто]'}")


@app.command("mic-test")
def mic_test(
    ctx: typer.Context,
    seconds: float = typer.Option(5.0, "--seconds", "-s", help="Длительность записи."),
    out: Path | None = typer.Option(None, "--out", help="Сохранить запись в WAV."),
) -> None:
    """Проверить микрофон и уровень сигнала."""
    config, paths = _load(ctx)
    setup_logging(config.log_level, paths.log_file, console=console)

    import numpy as np
    import soundfile as sf

    from .audio.input import record_seconds_checked

    console.print(f"[bold]Говорите[/bold] ({seconds:.0f} c)…")
    try:
        samples = record_seconds_checked(
            seconds, sample_rate=16000, device=config.audio.input_device
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float64)))))
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    console.print(f"RMS: [bold]{rms:.4f}[/bold]   peak: {peak:.4f}")
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        sf.write(out, samples, 16000)
        console.print(f"[green]Сохранено:[/green] {out}")
    if rms <= 1e-7:
        console.print(
            "[red]Микрофон возвращает тишину.[/red] Разрешите доступ: macOS — "
            "System Settings → Privacy & Security → Microphone; Windows — "
            "Settings → Privacy → Microphone."
        )
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# API server
# ---------------------------------------------------------------------------
@app.command()
def serve(
    ctx: typer.Context,
    host: str | None = typer.Option(None, "--host"),
    port: int | None = typer.Option(None, "--port"),
) -> None:
    """Запустить локальный API для GUI, плагинов и автоматизации."""
    config, paths = _load(ctx)
    setup_logging(config.log_level, paths.log_file, console=console)
    config.api.host = host or config.api.host
    config.api.port = port or config.api.port
    if not config.api.token:
        config.api.token = ensure_api_token(paths)

    async def run() -> None:
        import uvicorn

        from .api.app import create_app

        runtime = Runtime(config, paths, console=console)
        await runtime.start()
        application = create_app(runtime)
        server = uvicorn.Server(
            uvicorn.Config(
                application,
                host=config.api.host,
                port=config.api.port,
                log_level="warning",
                access_log=False,
            )
        )
        console.print(
            Panel(
                f"API:   [bold]http://{config.api.host}:{config.api.port}[/bold]\n"
                f"Token: [dim]{config.api.token}[/dim]\n\n"
                "GET /api/state   GET /api/events   POST /api/speak",
                title="Jarvis API",
            )
        )
        try:
            await server.serve()
        finally:
            await runtime.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        console.print("[dim]Остановлено.[/dim]")


@app.command("config")
def show_config(ctx: typer.Context) -> None:
    """Показать текущий конфиг (без токена API)."""
    config, _paths = _load(ctx)
    payload = config.model_dump(mode="json", exclude_none=True)
    payload.get("api", {}).pop("token", None)
    console.print_json(data=payload)


@app.command()
def version() -> None:
    """Версия Jarvis."""
    console.print(f"jarvis {__version__}")


if __name__ == "__main__":
    app()
