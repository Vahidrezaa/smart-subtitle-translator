"""Provider abstraction: typed request/response and error hierarchy.

Any LLM backend (OpenAI-compatible, Anthropic, Gemini, local servers, ...) is a
subclass of :class:`TranslationProvider` registered in ``app.providers``.
Transport-level failures are typed so the batch processor can retry exactly the
transient classes and fail fast on permanent ones.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from typing import Any


class ProviderError(Exception):
    """Base class for provider errors."""


class ProviderAuthError(ProviderError):
    """API key invalid or lacking permissions (HTTP 401/403)."""


class ProviderRateLimitError(ProviderError):
    """Rate limited (HTTP 429). Carries the server's Retry-After, if sent."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ProviderTransientError(ProviderError):
    """Temporary failure: network problem, timeout, 5xx — safe to retry."""


class ProviderBadResponseError(ProviderError):
    """The provider answered but the content is unusable (bad JSON, missing lines)."""


class ProviderConfigError(ProviderError):
    """Permanent request problem (HTTP 400/404) or invalid configuration."""


@dataclass
class ChatMessage:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class ProviderRequest:
    model: str
    messages: list[ChatMessage]
    temperature: float | None = None
    max_tokens: int | None = None
    json_mode: bool = False
    timeout: float = 60.0
    # Structured engine payload (previous/lines/upcoming/glossary/...) carried for
    # logging, offline providers and tests. Transport providers send `messages`.
    payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def without_json_mode(self) -> "ProviderRequest":
        return replace(self, json_mode=False)


@dataclass
class ProviderResponse:
    content: str
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


class TranslationProvider(ABC):
    """Abstract provider interface."""

    name: str = "base"
    display_name: str = "Base provider"

    @abstractmethod
    def translate(self, request: ProviderRequest) -> ProviderResponse:
        """Send one chat-completion request and return the raw text content."""

    @abstractmethod
    def validate_config(self) -> list[str]:
        """Return configuration problems (empty list = OK)."""

    def test_connection(self) -> tuple[bool, str]:
        """Cheap connectivity check: a minimal completion request."""
        problems = self.validate_config()
        if problems:
            return False, " | ".join(problems)
        request = ProviderRequest(
            model="",
            messages=[ChatMessage(role="user", content="ping")],
            temperature=0,
            max_tokens=16,
            timeout=20.0,
        )
        try:
            response = self.translate(request)
            model = response.model or "?"
            return True, f"OK — model: {model}"
        except ProviderError as exc:
            return False, str(exc)
        except Exception as exc:  # unexpected — still report honestly
            return False, f"Unexpected error: {exc}"
