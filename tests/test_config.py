from __future__ import annotations

from pathlib import Path

from jarvis.config import JarvisConfig, init_config, load_config, save_config
from jarvis.paths import AppPaths


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "logs",
    )


def test_defaults_are_local_and_jarvis() -> None:
    config = JarvisConfig()
    assert config.language == "ru"
    assert config.wake.engine == "vad-whisper"
    assert config.wake.phrase == "джарвис"
    assert config.wake.stt_model == "tiny"
    assert config.stt.engine == "faster-whisper"
    assert config.tts.ru.engine == "piper"
    assert config.tts.en.engine == "kokoro"
    assert config.tts.omnivoice_cpp.port == 47810
    assert config.opencode.mode == "spawn"


def test_roundtrip_and_token(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    config, config_path = init_config(paths)
    assert config_path.exists()
    assert paths.token_file.exists()
    assert paths.token_file.read_text(encoding="utf-8").strip()
    assert config.api.token is None
    assert config.language == "ru"

    loaded = load_config(config_path, paths)
    assert loaded.tts.ru.voice == config.tts.ru.voice
    assert loaded.api.token is None
    assert loaded.language == "ru"

    loaded.speech.max_chars = 123
    save_config(loaded, config_path, paths)
    assert load_config(config_path, paths).speech.max_chars == 123


def test_init_is_idempotent(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    first, _ = init_config(paths)
    token = paths.token_file.read_text(encoding="utf-8")
    second, _ = init_config(paths)
    assert first.api.token is None and second.api.token is None
    assert paths.token_file.read_text(encoding="utf-8") == token


def test_legacy_auto_language_migrates_to_ru(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    paths.config_file.write_text(
        'language = "ru"\n\n[stt]\nmodel = "small"\nlanguage = "auto"\n',
        encoding="utf-8",
    )

    config = load_config(paths.config_file, paths)
    assert config.language == "ru"
    assert not hasattr(config.stt, "language")


def test_legacy_explicit_language_is_preserved(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    paths.config_file.write_text(
        '[stt]\nmodel = "small"\nlanguage = "en"\n',
        encoding="utf-8",
    )

    config = load_config(paths.config_file, paths)
    assert config.language == "en"


def test_rewritten_config_has_no_stt_language(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    config, config_path = init_config(paths)
    config.language = "en"
    save_config(config, config_path, paths)

    text = config_path.read_text(encoding="utf-8")
    assert 'language = "en"' in text
    assert "[stt]" in text
    assert 'language = "auto"' not in text
