"""Shared fixtures: isolated data dir and a migrated test database."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.database.database_manager import DatabaseManager


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SST_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SST_API_KEY", raising=False)
    return tmp_path


@pytest.fixture()
def db(data_dir: Path):
    manager = DatabaseManager(data_dir / "test.db")
    manager.migrate()
    yield manager
    manager.close()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SST_API_KEY", raising=False)
