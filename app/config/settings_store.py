"""Load/save user settings as JSON in the app data directory.

Settings never contain API keys — those live in credential stores
(see app.config.credentials).
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, fields
from pathlib import Path

from app.config.app_config import (
    ApiConfig,
    CorrectionConfig,
    ExportConfig,
    MemoryConfig,
    Settings,
    TranslationConfig,
)
from app.config.paths import get_settings_path

log = logging.getLogger(__name__)

_SECTIONS = {
    "api": ApiConfig,
    "translation": TranslationConfig,
    "memory": MemoryConfig,
    "corrections": CorrectionConfig,
    "export": ExportConfig,
}


def load_settings(path: Path | None = None) -> Settings:
    path = path or get_settings_path()
    settings = Settings()
    if not path.exists():
        return settings
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read settings file %s (%s); using defaults", path, exc)
        return settings
    if isinstance(raw, dict):
        _apply(raw, settings)
    return settings


def save_settings(settings: Settings, path: Path | None = None) -> Path:
    path = path or get_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {name: asdict(getattr(settings, name)) for name in _SECTIONS}
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _apply(raw: dict, settings: Settings) -> None:
    for section, cls in _SECTIONS.items():
        if not isinstance(raw.get(section), dict):
            continue
        target = getattr(settings, section)
        valid = {f.name for f in fields(cls)}
        for key, value in raw[section].items():
            if key not in valid:
                continue
            current = getattr(target, key)
            try:
                setattr(target, key, type(current)(value))
            except (TypeError, ValueError):
                log.warning("Ignoring invalid setting %s.%s=%r", section, key, value)
