from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT_DIR / "config" / "dashboard.yaml"
_ENV_PATTERN = re.compile(r"\$\{([A-Z][A-Z0-9_]*)(?::-([^}]*))?}")


def _expand_environment(raw: str) -> str:
    """Expand ${NAME} and ${NAME:-fallback} placeholders in YAML text."""

    def replace(match: re.Match[str]) -> str:
        name, fallback = match.group(1), match.group(2)
        value = os.getenv(name)
        if value is not None:
            return value
        return fallback or ""

    return _ENV_PATTERN.sub(replace, raw)


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    load_dotenv(ROOT_DIR / ".env", override=False)
    selected = Path(path or os.getenv("DASHBOARD_CONFIG", DEFAULT_CONFIG_PATH))
    if not selected.is_absolute():
        selected = ROOT_DIR / selected
    if not selected.exists():
        raise FileNotFoundError(f"Dashboard-Konfiguration fehlt: {selected}")

    data = yaml.safe_load(_expand_environment(selected.read_text(encoding="utf-8"))) or {}
    data["_config_path"] = str(selected)
    data["_root_dir"] = str(ROOT_DIR)
    return data


def resolve_project_path(config: dict[str, Any], value: str | Path) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate
    return Path(config["_root_dir"]) / candidate

