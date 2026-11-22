"""Credential storage for API keys.

The key is never hardcoded and never written into settings.json. Resolution
order (backend "auto"): environment variable -> OS keyring (Windows Credential
Manager via the keyring package) -> an explicitly opted-in local file, which is
clearly marked as the least secure option.
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path

from app.config.paths import get_data_dir

log = logging.getLogger(__name__)

SERVICE_NAME = "SmartSubtitleTranslator"
ENV_VAR_NAME = "SST_API_KEY"


class CredentialError(RuntimeError):
    """Raised when no usable API key is configured."""


class CredentialStore(ABC):
    name = "base"

    @abstractmethod
    def get_key(self) -> str | None: ...

    @abstractmethod
    def set_key(self, key: str) -> None: ...

    @abstractmethod
    def delete_key(self) -> None: ...

    @abstractmethod
    def is_available(self) -> bool: ...


class EnvCredentialStore(CredentialStore):
    name = "env"

    def is_available(self) -> bool:
        return bool(os.environ.get(ENV_VAR_NAME))

    def get_key(self) -> str | None:
        return os.environ.get(ENV_VAR_NAME) or None

    def set_key(self, key: str) -> None:
        os.environ[ENV_VAR_NAME] = key

    def delete_key(self) -> None:
        os.environ.pop(ENV_VAR_NAME, None)


class KeyringCredentialStore(CredentialStore):
    """OS keyring (Windows Credential Manager on Windows). Optional dependency."""

    name = "keyring"

    def __init__(self) -> None:
        self._keyring = None
        self._usable: bool | None = None

    def is_available(self) -> bool:
        if self._usable is None:
            try:
                import keyring  # type: ignore

                self._keyring = keyring
                keyring.get_password(SERVICE_NAME, "probe")
                self._usable = True
            except Exception:
                self._usable = False
        return self._usable

    def get_key(self) -> str | None:
        if not self.is_available():
            return None
        try:
            return self._keyring.get_password(SERVICE_NAME, "api_key")
        except Exception as exc:
            log.warning("Keyring read failed: %s", exc)
            return None

    def set_key(self, key: str) -> None:
        if not self.is_available():
            raise CredentialError("No usable system keyring backend is available")
        self._keyring.set_password(SERVICE_NAME, "api_key", key)

    def delete_key(self) -> None:
        if not self.is_available():
            return
        try:
            self._keyring.delete_password(SERVICE_NAME, "api_key")
        except Exception:
            pass


class FileCredentialStore(CredentialStore):
    """Least-secure fallback, used only when explicitly selected or when no
    keyring backend exists. The file lives in the app data directory."""

    name = "file"

    def __init__(self, directory: Path | None = None) -> None:
        self._path = (directory or get_data_dir()) / "api_key.txt"

    def is_available(self) -> bool:
        return True

    def get_key(self) -> str | None:
        try:
            if self._path.exists():
                return self._path.read_text(encoding="utf-8").strip() or None
        except OSError as exc:
            log.warning("Credential file read failed: %s", exc)
        return None

    def set_key(self, key: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(key, encoding="utf-8")
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    def delete_key(self) -> None:
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass


def get_store(backend: str = "auto") -> CredentialStore:
    backend = (backend or "auto").lower()
    if backend == "env":
        return EnvCredentialStore()
    if backend == "keyring":
        return KeyringCredentialStore()
    if backend == "file":
        return FileCredentialStore()
    # auto: env first, then OS keyring, then file
    env = EnvCredentialStore()
    if env.is_available():
        return env
    keyring_store = KeyringCredentialStore()
    if keyring_store.is_available():
        return keyring_store
    return FileCredentialStore()


def resolve_api_key(backend: str = "auto") -> str:
    """Return the configured API key or raise CredentialError. Never logs the key."""
    key = get_store(backend).get_key()
    if not key:
        raise CredentialError(
            "No API key configured. Set it in API Settings, or via the "
            f"{ENV_VAR_NAME} environment variable."
        )
    return key
