"""Platform-aware paths for persistent application data.

Resolution order for the data directory:
1. ``SST_DATA_DIR`` environment variable (tests, portable installs);
2. ``%APPDATA%\\SmartSubtitleTranslator`` on Windows;
3. ``~/.local/share/SmartSubtitleTranslator`` elsewhere.
"""
from __future__ import annotations

import os
from pathlib import Path

APP_DIR_NAME = "SmartSubtitleTranslator"


def get_data_dir() -> Path:
    env = os.environ.get("SST_DATA_DIR")
    if env:
        return Path(env)
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / APP_DIR_NAME
    return Path.home() / ".local" / "share" / APP_DIR_NAME


def get_database_path() -> Path:
    return get_data_dir() / "subtitle_translator.db"


def get_settings_path() -> Path:
    return get_data_dir() / "settings.json"
