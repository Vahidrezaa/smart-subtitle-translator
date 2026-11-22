"""Provider registry.

Add a backend by subclassing TranslationProvider and decorating with
``register_provider``; the GUI settings dialog and the engine discover it here.
"""
from __future__ import annotations

from app.providers.base_provider import (
    ChatMessage,
    ProviderAuthError,
    ProviderBadResponseError,
    ProviderConfigError,
    ProviderError,
    ProviderRateLimitError,
    ProviderRequest,
    ProviderResponse,
    ProviderTransientError,
    TranslationProvider,
)
from app.providers.offline_provider import OfflineDebugProvider
from app.providers.openai_provider import OpenAICompatibleProvider

_REGISTRY: dict[str, type[TranslationProvider]] = {}


def register_provider(provider_class: type[TranslationProvider]) -> type[TranslationProvider]:
    _REGISTRY[provider_class.name] = provider_class
    return provider_class


def create_provider(name: str, **kwargs) -> TranslationProvider:
    try:
        provider_class = _REGISTRY[name]
    except KeyError:
        raise ProviderConfigError(
            f"Unknown provider {name!r}; available: {sorted(_REGISTRY)}"
        ) from None
    return provider_class(**kwargs)


def available_providers() -> dict[str, type[TranslationProvider]]:
    return dict(_REGISTRY)


register_provider(OpenAICompatibleProvider)
register_provider(OfflineDebugProvider)

__all__ = [
    "ChatMessage",
    "OfflineDebugProvider",
    "OpenAICompatibleProvider",
    "ProviderAuthError",
    "ProviderBadResponseError",
    "ProviderConfigError",
    "ProviderError",
    "ProviderRateLimitError",
    "ProviderRequest",
    "ProviderResponse",
    "ProviderTransientError",
    "TranslationProvider",
    "available_providers",
    "create_provider",
    "register_provider",
]
