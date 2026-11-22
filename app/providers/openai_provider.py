"""OpenAI-compatible chat-completions provider.

Works with OpenAI and any compatible endpoint (OpenRouter, Azure OpenAI
gateways, LM Studio, llama.cpp server, vLLM, ...). Uses `requests` directly so
the endpoint URL is fully configurable and no SDK pins us to one vendor.

If a server rejects ``response_format`` (JSON mode), the request is retried
once without it — JSON discipline is then enforced by the engine's parser.
"""
from __future__ import annotations

import logging

import requests

from app.providers.base_provider import (
    ProviderAuthError,
    ProviderBadResponseError,
    ProviderConfigError,
    ProviderRateLimitError,
    ProviderRequest,
    ProviderResponse,
    ProviderTransientError,
    TranslationProvider,
)

log = logging.getLogger(__name__)

_RETRYABLE_HTTP = {408, 409, 425, 500, 502, 503, 504}


class OpenAICompatibleProvider(TranslationProvider):
    name = "openai"
    display_name = "OpenAI-compatible"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.2,
        timeout: float = 60.0,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.session = session or requests.Session()

    # ------------------------------------------------------------- validation
    def validate_config(self) -> list[str]:
        problems = []
        if not self.base_url:
            problems.append("Base URL is empty.")
        elif not self.base_url.startswith(("http://", "https://")):
            problems.append("Base URL must start with http:// or https://.")
        if not self.model:
            problems.append("Model name is empty.")
        if not self.api_key:
            problems.append("API key is not configured.")
        return problems

    # ---------------------------------------------------------------- request
    def _endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    @staticmethod
    def _headers(api_key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    @staticmethod
    def _body(request: ProviderRequest, default_model: str) -> dict:
        body: dict = {
            "model": request.model or default_model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        }
        temperature = request.temperature if request.temperature is not None else None
        if temperature is not None:
            body["temperature"] = temperature
        if request.max_tokens:
            body["max_tokens"] = request.max_tokens
        if request.json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    def translate(self, request: ProviderRequest) -> ProviderResponse:
        problems = self.validate_config()
        # allow an explicit per-request model override to satisfy validation
        if request.model and "Model name is empty." in problems:
            problems.remove("Model name is empty.")
        if problems:
            raise ProviderConfigError(" | ".join(problems))

        timeout = request.timeout or self.timeout
        try:
            response = self.session.post(
                self._endpoint(),
                json=self._body(request, self.model),
                headers=self._headers(self.api_key),
                timeout=timeout,
            )
        except requests.Timeout as exc:
            raise ProviderTransientError(
                f"Request timed out after {timeout:g}s"
            ) from exc
        except requests.ConnectionError as exc:
            raise ProviderTransientError(f"Connection failed: {exc}") from exc

        if response.status_code >= 400:
            return self._raise_for_status(response, request)

        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderBadResponseError(
                f"Response body is not valid JSON (HTTP {response.status_code})"
            ) from exc
        return self._parse(data, request.model or self.model)

    # ----------------------------------------------------------------- errors
    def _raise_for_status(
        self, response: requests.Response, request: ProviderRequest
    ) -> ProviderResponse:
        code = response.status_code
        try:
            data = response.json()
            error = data.get("error") if isinstance(data, dict) else None
            detail = (error or {}).get("message") if isinstance(error, dict) else None
            detail = detail or str(data)[:300]
        except ValueError:
            detail = response.text[:300]

        if code in (401, 403):
            raise ProviderAuthError(f"Authentication failed (HTTP {code}): {detail}")
        if code == 429:
            retry_after = 0.0
            header = response.headers.get("Retry-After")
            if header:
                try:
                    retry_after = float(header)
                except ValueError:
                    retry_after = 0.0
            raise ProviderRateLimitError(
                f"Rate limited (HTTP 429): {detail}", retry_after=retry_after
            )
        if code in _RETRYABLE_HTTP:
            raise ProviderTransientError(f"Transient server error (HTTP {code}): {detail}")
        if code == 400 and request.json_mode and "response_format" in detail.lower():
            log.info("Endpoint rejected response_format; retrying once without JSON mode")
            return self.translate(request.without_json_mode())
        raise ProviderConfigError(f"Provider rejected the request (HTTP {code}): {detail}")

    # ----------------------------------------------------------------- parsing
    @staticmethod
    def _parse(data: dict, model: str) -> ProviderResponse:
        choices = data.get("choices") or []
        if not choices:
            raise ProviderBadResponseError(f"Response has no choices: {str(data)[:200]}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not content or not str(content).strip():
            raise ProviderBadResponseError("Response content is empty")
        usage = data.get("usage") or {}
        return ProviderResponse(
            content=str(content),
            model=str(data.get("model") or model),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            raw=data if isinstance(data, dict) else {},
        )
