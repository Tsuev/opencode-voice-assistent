"""Import cloned voices (reference audio + transcript) into Jarvis.

A cloned voice is just a short reference recording plus its exact transcript.
Nothing here depends on VoiceStudio at runtime: the importer reads
VoiceStudio's local SQLite database once and copies the reference into Jarvis'
own data directory.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..paths import AppPaths

REFERENCE_AUDIO = "reference.wav"
REFERENCE_TEXT = "reference.txt"
PROFILE_JSON = "profile.json"
SAMPLE_RATE = 24000


class VoiceImportError(RuntimeError):
    pass


@dataclass(slots=True)
class VoiceAsset:
    name: str
    directory: Path
    reference_audio: Path
    reference_text: str
    profile: dict[str, Any]


def slugify(name: str) -> str:
    value = re.sub(r"[^\w.-]+", "-", name.strip().lower(), flags=re.UNICODE)
    value = value.strip("-._")
    return value or "jarvis"


def voice_dir(paths: AppPaths, name: str) -> Path:
    return paths.voices_dir / slugify(name)


def voicestudio_data_dir() -> Path | None:
    """Best-effort VoiceStudio data directory on Windows/macOS/Linux."""
    override = os.environ.get("OMNIVOICE_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        return Path(base) / "OmniVoice" if base else None
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "OmniVoice"
    return Path.home() / ".omnivoice"


def voicestudio_db_path() -> Path | None:
    data_dir = voicestudio_data_dir()
    if data_dir is None:
        return None
    db = data_dir / "omnivoice.db"
    return db if db.exists() else None


def _connect_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def find_voicestudio_profile(name: str, *, db_path: Path | None = None) -> dict[str, Any] | None:
    db_path = db_path or voicestudio_db_path()
    if db_path is None:
        return None
    with _connect_readonly(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM voice_profiles WHERE lower(name) = lower(?) "
            "ORDER BY created_at DESC LIMIT 1",
            (name.strip(),),
        ).fetchone()
    return dict(row) if row is not None else None


def _reference_path(profile: dict[str, Any], data_dir: Path) -> Path:
    voices_dir = data_dir / "voices"
    locked = profile.get("locked_audio_path") or ""
    if profile.get("is_locked") and locked:
        return voices_dir / locked
    relative = profile.get("ref_audio_path") or ""
    if not relative:
        raise VoiceImportError(
            f"У профиля «{profile.get('name', '?')}» нет reference-аудио. "
            "Пересоздайте клон в VoiceStudio или укажите файл вручную."
        )
    return voices_dir / relative


def _write_reference_audio(source: Path, destination: Path) -> None:
    """Decode any soundfile-supported format and store 24 kHz mono PCM16."""
    try:
        import numpy as np
        import soundfile as sf
        import soxr
    except ImportError as exc:  # pragma: no cover - dependencies are in pyproject
        raise VoiceImportError("Для импорта голоса нужны soundfile и soxr") from exc

    if not source.exists():
        raise VoiceImportError(f"Файл референса не найден: {source}")

    data, sample_rate = sf.read(str(source), dtype="float32", always_2d=True)
    if data.size == 0:
        raise VoiceImportError(f"Пустой аудиофайл: {source}")
    mono = np.asarray(data, dtype=np.float32).mean(axis=1)
    if int(sample_rate) != SAMPLE_RATE:
        mono = soxr.resample(mono, int(sample_rate), SAMPLE_RATE)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(".wav.part")
    sf.write(
        str(partial),
        np.asarray(mono, dtype=np.float32),
        SAMPLE_RATE,
        format="WAV",
        subtype="PCM_16",
    )
    os.replace(partial, destination)


def _write_asset(
    paths: AppPaths,
    *,
    target_name: str,
    reference_source: Path,
    reference_text: str,
    profile: dict[str, Any],
) -> VoiceAsset:
    reference_text = reference_text.strip()
    if not reference_text:
        raise VoiceImportError(
            "Не найден текст референса. Без него клонирование будет неточным; "
            "укажите его через --text."
        )
    directory = voice_dir(paths, target_name)
    directory.mkdir(parents=True, exist_ok=True)
    audio_path = directory / REFERENCE_AUDIO
    _write_reference_audio(reference_source, audio_path)

    text_path = directory / REFERENCE_TEXT
    tmp_text = text_path.with_suffix(".txt.part")
    tmp_text.write_text(reference_text + "\n", encoding="utf-8")
    os.replace(tmp_text, text_path)

    payload = dict(profile)
    payload.setdefault("name", target_name)
    payload["jarvis_voice"] = slugify(target_name)
    payload["reference_audio"] = str(audio_path)
    payload["reference_text"] = reference_text
    payload.setdefault("source", "file")
    profile_path = directory / PROFILE_JSON
    tmp_profile = profile_path.with_suffix(".json.part")
    tmp_profile.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_profile, profile_path)

    return VoiceAsset(
        name=slugify(target_name),
        directory=directory,
        reference_audio=audio_path,
        reference_text=reference_text,
        profile=payload,
    )


def import_from_voicestudio(
    name: str,
    paths: AppPaths,
    *,
    target: str | None = None,
) -> VoiceAsset:
    data_dir = voicestudio_data_dir()
    profile = find_voicestudio_profile(name)
    if data_dir is None or profile is None:
        raise VoiceImportError(
            f"Профиль «{name}» не найден в VoiceStudio. "
            "Укажите файл вручную: --audio ref.wav --text ref.txt"
        )
    source = _reference_path(profile, data_dir)
    profile["source"] = "voicestudio"
    profile["source_profile_id"] = profile.get("id")
    profile["source_audio"] = str(source)
    return _write_asset(
        paths,
        target_name=target or name,
        reference_source=source,
        reference_text=str(profile.get("ref_text") or ""),
        profile=profile,
    )


def import_from_file(
    name: str,
    audio: Path,
    text: str | Path,
    paths: AppPaths,
    *,
    target: str | None = None,
) -> VoiceAsset:
    text_value = text.read_text(encoding="utf-8") if isinstance(text, Path) else str(text)
    return _write_asset(
        paths,
        target_name=target or name,
        reference_source=Path(audio).expanduser(),
        reference_text=text_value,
        profile={"name": name, "source": "file"},
    )


def load_asset(name: str, paths: AppPaths) -> VoiceAsset:
    directory = voice_dir(paths, name)
    audio_path = directory / REFERENCE_AUDIO
    text_path = directory / REFERENCE_TEXT
    if not audio_path.exists() or not text_path.exists():
        raise VoiceImportError(
            f"Голос «{name}» не импортирован. Укажите референс-аудио и его "
            "точный текст: jarvis voices import --name "
            f"{name} --from file --audio ref.wav --text ref.txt "
            "(или --from assets, если есть assets/ref.mp3 и ref.txt). "
            "VoiceStudio нужен только при явном --from voicestudio."
        )
    profile_path = directory / PROFILE_JSON
    profile: dict[str, Any] = {}
    if profile_path.exists():
        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            profile = {}
    return VoiceAsset(
        name=slugify(name),
        directory=directory,
        reference_audio=audio_path,
        reference_text=text_path.read_text(encoding="utf-8").strip(),
        profile=profile,
    )


def list_assets(paths: AppPaths) -> list[VoiceAsset]:
    if not paths.voices_dir.exists():
        return []
    assets: list[VoiceAsset] = []
    for directory in sorted(paths.voices_dir.iterdir()):
        if not directory.is_dir():
            continue
        if not (directory / REFERENCE_AUDIO).exists():
            continue
        try:
            assets.append(load_asset(directory.name, paths))
        except VoiceImportError:
            continue
    return assets
