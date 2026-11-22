"""Core exception types shared across the pipeline."""
from __future__ import annotations


class TranslationError(Exception):
    """Base class for translation pipeline errors."""


class BatchCancelled(TranslationError):
    """Raised when the user cancels a batch run (state is already checkpointed)."""


class ConfigurationError(TranslationError):
    """Raised when required configuration (provider, key, ...) is missing or invalid."""
