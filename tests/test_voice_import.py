from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import soundfile as sf

from jarvis.paths import AppPaths
from jarvis.tts.voice_import import (
    import_from_file,
    import_from_voicestudio,
    list_assets,
    load_asset,
)


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "logs",
    )


def _write_wav(path: Path, seconds: float = 0.5, sample_rate: int = 44100) -> None:
    samples = np.zeros(int(seconds * sample_rate), dtype=np.float32)
    sf.write(path, samples, sample_rate, subtype="PCM_16")


def test_import_from_file_converts_to_24k_mono(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    source = tmp_path / "ref.wav"
    _write_wav(source)
    text = "Это тестовый референс."

    asset = import_from_file("JARVIS", source, text, paths, target="jarvis")

    assert asset.name == "jarvis"
    assert asset.reference_audio.exists()
    info = sf.info(asset.reference_audio)
    assert info.samplerate == 24000
    assert info.channels == 1
    assert load_asset("jarvis", paths).reference_text == text
    assert [item.name for item in list_assets(paths)] == ["jarvis"]
    profile = json.loads((asset.directory / "profile.json").read_text(encoding="utf-8"))
    assert profile["source"] == "file"


def test_import_from_voicestudio_reads_reference(tmp_path: Path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    data_dir = tmp_path / "OmniVoice"
    voices = data_dir / "voices"
    voices.mkdir(parents=True)
    reference = voices / "ref.wav"
    _write_wav(reference)

    db_path = data_dir / "omnivoice.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE voice_profiles (
            id TEXT PRIMARY KEY,
            name TEXT,
            ref_audio_path TEXT,
            ref_text TEXT,
            locked_audio_path TEXT,
            is_locked INTEGER DEFAULT 0,
            language TEXT,
            created_at REAL
        )
        """
    )
    connection.execute(
        "INSERT INTO voice_profiles VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "abc123",
            "JARVIS",
            "ref.wav",
            "Мой настоящий голос.",
            "",
            0,
            "Auto",
            1.0,
        ),
    )
    connection.commit()
    connection.close()

    monkeypatch.setenv("OMNIVOICE_DATA_DIR", str(data_dir))

    asset = import_from_voicestudio("JARVIS", paths)

    assert asset.name == "jarvis"
    assert asset.reference_text == "Мой настоящий голос."
    assert asset.profile["source"] == "voicestudio"
    assert asset.profile["source_profile_id"] == "abc123"
