"""Tests for settings persistence and credential stores."""
from __future__ import annotations

from app.config.app_config import Settings
from app.config.credentials import (
    CredentialError,
    EnvCredentialStore,
    FileCredentialStore,
    get_store,
    resolve_api_key,
)
from app.config.settings_store import load_settings, save_settings


class TestSettingsStore:
    def test_defaults_when_missing(self, data_dir):
        settings = load_settings()
        assert settings.api.provider == "openai"
        assert settings.translation.target_language == "fa"
        assert settings.translation.batch_max_lines == 15

    def test_save_load_roundtrip(self, data_dir):
        settings = Settings()
        settings.api.model = "gpt-4.1-mini"
        settings.api.temperature = 0.4
        settings.translation.batch_max_lines = 25
        settings.memory.reuse_enabled = False
        settings.export.bom = False
        path = save_settings(settings)
        loaded = load_settings(path)
        assert loaded.api.model == "gpt-4.1-mini"
        assert loaded.api.temperature == 0.4
        assert loaded.translation.batch_max_lines == 25
        assert loaded.memory.reuse_enabled is False
        assert loaded.export.bom is False

    def test_invalid_and_unknown_values_ignored(self, data_dir):
        import json

        raw = {
            "api": {"temperature": "not-a-number", "bogus_key": 1},
            "translation": {"batch_max_lines": "not-a-number", "target_language": "fa"},
        }
        path = data_dir / "settings.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        settings = load_settings(path)
        assert settings.api.temperature == 0.2  # default kept
        assert settings.translation.batch_max_lines == 15  # default kept
        assert settings.translation.target_language == "fa"  # valid value applied


class TestCredentialStores:
    def test_env_store(self, monkeypatch):
        store = EnvCredentialStore()
        monkeypatch.setenv("SST_API_KEY", "sk-test-123")
        assert store.is_available()
        assert store.get_key() == "sk-test-123"
        monkeypatch.delenv("SST_API_KEY")
        assert not store.is_available()
        assert store.get_key() is None

    def test_file_store_roundtrip(self, data_dir):
        store = FileCredentialStore(data_dir)
        assert store.get_key() is None
        store.set_key("sk-file-key")
        assert store.get_key() == "sk-file-key"
        store.delete_key()
        assert store.get_key() is None

    def test_auto_prefers_env(self, data_dir, monkeypatch):
        monkeypatch.setenv("SST_API_KEY", "sk-env")
        assert get_store("auto").name == "env"

    def test_resolve_api_key_raises_when_missing(self, data_dir, monkeypatch):
        monkeypatch.delenv("SST_API_KEY", raising=False)
        try:
            resolve_api_key("file")
        except CredentialError:
            pass
        else:
            raise AssertionError("expected CredentialError with no key configured")

    def test_resolve_api_key_from_file(self, data_dir, monkeypatch):
        monkeypatch.delenv("SST_API_KEY", raising=False)
        FileCredentialStore(data_dir).set_key("sk-file")
        assert resolve_api_key("file") == "sk-file"
