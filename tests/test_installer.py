from __future__ import annotations

import hashlib
import io
from pathlib import Path

import httpx
import pytest
from rich.console import Console

from jarvis import installer
from jarvis.config import load_config
from jarvis.installer import InstallError, fetch_omnivoice_models, install
from jarvis.paths import AppPaths, get_paths


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "logs",
    )


def _manifest(base: bytes, codec: bytes) -> dict:
    def item(name: str, data: bytes) -> dict:
        return {
            "name": name,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    return {
        "repo": "example/omnivoice",
        "revision": "main",
        "variants": {
            "q8": {
                "base": item("base.gguf", base),
                "tokenizer": item("codec.gguf", codec),
            }
        },
    }


def test_fetch_models_offline_adopts_local_files(tmp_path: Path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    base = b"base-model"
    codec = b"codec-model"
    monkeypatch.setattr(installer, "engine_manifest", lambda: _manifest(base, codec))
    local = tmp_path / "models"
    local.mkdir()
    (local / "base.gguf").write_bytes(base)
    (local / "codec.gguf").write_bytes(codec)
    monkeypatch.chdir(tmp_path)

    base_path, codec_path = fetch_omnivoice_models(
        paths,
        variant="q8",
        directory=tmp_path / "data" / "models" / "omnivoice-cpp",
        offline=True,
    )

    assert base_path.read_bytes() == base
    assert codec_path.read_bytes() == codec
    assert (tmp_path / "data" / "models" / "omnivoice-cpp" / "base.gguf").exists()


def test_fetch_models_offline_reports_missing(tmp_path: Path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    monkeypatch.setattr(
        installer,
        "engine_manifest",
        lambda: _manifest(b"base-model", b"codec-model"),
    )

    with pytest.raises(InstallError, match="Offline"):
        fetch_omnivoice_models(paths, offline=True)


def test_fetch_models_downloads_and_verifies(tmp_path: Path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    base = b"downloaded-base"
    codec = b"downloaded-codec"
    monkeypatch.setattr(installer, "engine_manifest", lambda: _manifest(base, codec))

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, content=base if name == "base.gguf" else codec)

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://huggingface.co"
    )
    base_path, codec_path = fetch_omnivoice_models(
        paths,
        directory=tmp_path / "models",
        accept_license=True,
        client=client,
    )

    assert base_path.read_bytes() == base
    assert codec_path.read_bytes() == codec


def test_reference_assets_finds_audio_and_text(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "ref.txt").write_text("текст", encoding="utf-8")
    (assets / "ref.mp3").write_bytes(b"fake")

    found = installer.reference_assets(assets)

    assert found is not None
    assert found[0].name == "ref.mp3"
    assert found[1].name == "ref.txt"


def test_fast_profile_does_not_require_voice_studio(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    install(
        profile="fast",
        offline=True,
        warmup=False,
        console=Console(file=io.StringIO()),
    )

    config = load_config(paths=get_paths())
    assert config.tts.ru.engine == "piper"
    assert config.tts.en.engine == "kokoro"
    assert config.wake.engine == "vad-whisper"
