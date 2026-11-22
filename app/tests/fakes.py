"""Test doubles: fake HTTP session, fake/scripted providers (no network needed)."""
from __future__ import annotations

import json
from typing import Any

from app.providers.base_provider import (
    ProviderRequest,
    ProviderResponse,
    ProviderTransientError,
    TranslationProvider,
)


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        json_data: Any = None,
        headers: dict | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._json = json_data
        self.headers = headers or {}
        self.text = text

    def json(self) -> Any:
        if self._json is None:
            raise ValueError("no JSON body")
        return self._json


class FakeSession:
    """Stands in for requests.Session. Pass one FakeResponse or a list of them
    (one per call); pass an Exception to simulate transport errors."""

    def __init__(self, response: Any) -> None:
        self._responses = response if isinstance(response, list) else [response]
        self.calls: list[dict] = []

    def post(self, url: str, json: dict | None = None, headers: dict | None = None,
             timeout: float | None = None) -> FakeResponse:
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        item = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(item, Exception):
            raise item
        return item


class FakeProvider(TranslationProvider):
    """Answers every request with deterministic placeholder Persian.

    Records all requests so tests can assert batching / reuse behavior."""

    name = "fake"

    def __init__(self, fail_times: int = 0, model: str = "fake-model",
                 error: Exception | None = None) -> None:
        self.calls: list[ProviderRequest] = []
        self.fail_times = fail_times
        self.model_name = model
        self.error = error

    def validate_config(self) -> list[str]:
        return []

    def translate(self, request: ProviderRequest) -> ProviderResponse:
        self.calls.append(request)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.error or ProviderTransientError("simulated transient failure")
        lines = (request.payload or {}).get("lines", [])
        translations = [{"i": line["i"], "fa": f"ترجمه {line['en']}"} for line in lines]
        content = json.dumps({"translations": translations}, ensure_ascii=False)
        return ProviderResponse(
            content=content,
            model=self.model_name,
            prompt_tokens=10,
            completion_tokens=20,
        )


class ScriptedProvider(TranslationProvider):
    """Returns scripted raw-content strings (or raises scripted exceptions) in order."""

    name = "scripted"

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[ProviderRequest] = []

    def validate_config(self) -> list[str]:
        return []

    def translate(self, request: ProviderRequest) -> ProviderResponse:
        self.calls.append(request)
        if not self.responses:
            raise ProviderTransientError("no scripted response left")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return ProviderResponse(content=item, model="scripted-model")
