"""Filesystem locations used by Jarvis.

The application keeps a strict separation between:

* ``config``  - user editable TOML + API token
* ``data``    - downloaded models and voices
* ``cache``   - disposable caches
* ``logs``    - rotating log files

A single ``JARVIS_HOME`` environment variable can relocate all four
directories, which is heavily used by the test suite and by portable installs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_cache_dir, user_config_dir, user_data_dir, user_log_dir

APP_NAME = "jarvis"


@dataclass(frozen=True, slots=True)
class AppPaths:
    config_dir: Path
    data_dir: Path
    cache_dir: Path
    log_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def token_file(self) -> Path:
        return self.config_dir / "token"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def bin_dir(self) -> Path:
        return self.data_dir / "bin"

    @property
    def voices_dir(self) -> Path:
        return self.models_dir / "voices"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "jarvis.log"

    def ensure(self) -> AppPaths:
        for directory in (self.config_dir, self.data_dir, self.cache_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)
        return self


def get_paths() -> AppPaths:
    """Return application paths, honouring ``JARVIS_HOME`` override."""
    home = os.environ.get("JARVIS_HOME")
    if home:
        root = Path(home).expanduser().resolve()
        return AppPaths(
            config_dir=root / "config",
            data_dir=root / "data",
            cache_dir=root / "cache",
            log_dir=root / "logs",
        )
    return AppPaths(
        config_dir=Path(user_config_dir(APP_NAME)),
        data_dir=Path(user_data_dir(APP_NAME)),
        cache_dir=Path(user_cache_dir(APP_NAME)),
        log_dir=Path(user_log_dir(APP_NAME)),
    )
