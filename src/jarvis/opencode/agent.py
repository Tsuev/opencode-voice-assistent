"""Install the OpenCode agent definition that makes replies speech-friendly."""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path

AGENT_FILENAME = "jarvis.md"

_LANGUAGE_RULES = {
    "ru": ("Всегда отвечай по-русски, даже если в команде есть английские технические термины."),
    "en": "Always answer in English, even if the command contains Russian words.",
}


def agent_source(language: str = "ru") -> str:
    resource = resources.files("jarvis").joinpath("resources", AGENT_FILENAME)
    template = resource.read_text(encoding="utf-8")
    rule = _LANGUAGE_RULES.get(language, _LANGUAGE_RULES["ru"])
    return template.replace("{{LANGUAGE_RULE}}", rule)


def opencode_config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "opencode"


def agent_target(*, project: str | Path | None = None) -> Path:
    if project is not None:
        return Path(project).expanduser().resolve() / ".opencode" / "agent" / AGENT_FILENAME
    return opencode_config_dir() / "agent" / AGENT_FILENAME


def install_agent(
    *,
    language: str = "ru",
    project: str | Path | None = None,
    force: bool = False,
) -> tuple[Path, bool]:
    """Write the agent file. Returns ``(path, written)``."""
    target = agent_target(project=project)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not force:
        return target, False
    target.write_text(agent_source(language), encoding="utf-8")
    return target, True
