"""Offline debug provider — NOT a real translator.

Produces deterministic placeholder output that mimics the API contract, so the
whole pipeline (batching, memory, corrections review, export) can be exercised
without an API key or network. The GUI exposes it as "Offline (debug)" for
exactly that purpose; it never produces usable Persian translations.
"""
from __future__ import annotations

import json

from app.providers.base_provider import (
    ProviderRequest,
    ProviderResponse,
    ProviderTransientError,
    TranslationProvider,
)


class OfflineDebugProvider(TranslationProvider):
    name = "offline"
    display_name = "Offline (debug — no real translation)"

    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times

    def validate_config(self) -> list[str]:
        return []

    def translate(self, request: ProviderRequest) -> ProviderResponse:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ProviderTransientError("Simulated transient failure (offline debug provider)")
        payload = request.payload or {}
        if "captions" in payload:  # corrections-review contract
            reviews = [
                {"i": cap["i"], "suspect": False, "correction": cap["text"],
                 "confidence": 0.0, "reason": "offline debug provider does not review"}
                for cap in payload["captions"]
            ]
            content = json.dumps({"reviews": reviews}, ensure_ascii=False)
        else:
            translations = [
                {"i": line["i"], "fa": f"[آفلاین] {line['en']}"}
                for line in payload.get("lines", [])
            ]
            content = json.dumps({"translations": translations}, ensure_ascii=False)
        return ProviderResponse(content=content, model="offline-debug", prompt_tokens=0,
                                completion_tokens=0)
