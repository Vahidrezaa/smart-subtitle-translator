"""Tests for the OpenAI-compatible provider over a fake HTTP session."""
from __future__ import annotations

import requests

from app.providers.base_provider import (
    ChatMessage,
    ProviderAuthError,
    ProviderBadResponseError,
    ProviderConfigError,
    ProviderRateLimitError,
    ProviderRequest,
    ProviderTransientError,
)
from app.providers.offline_provider import OfflineDebugProvider
from app.providers.openai_provider import OpenAICompatibleProvider
from app.providers import available_providers, create_provider

from app.tests.fakes import FakeResponse, FakeSession

GOOD_BODY = {
    "choices": [{"message": {"content": "{\"translations\": []}"}}],
    "model": "gpt-4o-mini",
    "usage": {"prompt_tokens": 11, "completion_tokens": 7},
}


def _provider(session) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        base_url="https://api.example.com/v1",
        api_key="sk-test",
        model="gpt-4o-mini",
        temperature=0.3,
        timeout=30,
        session=session,
    )


def _request(json_mode: bool = True) -> ProviderRequest:
    return ProviderRequest(
        model="gpt-4o-mini",
        messages=[ChatMessage(role="system", content="sys"), ChatMessage(role="user", content="usr")],
        temperature=0.3,
        json_mode=json_mode,
        timeout=30,
    )


class TestHappyPath:
    def test_success_response(self):
        session = FakeSession(FakeResponse(json_data=GOOD_BODY))
        provider = _provider(session)
        response = provider.translate(_request())
        assert response.content == "{\"translations\": []}"
        assert response.model == "gpt-4o-mini"
        assert response.prompt_tokens == 11
        assert response.completion_tokens == 7

    def test_request_shape(self):
        session = FakeSession(FakeResponse(json_data=GOOD_BODY))
        provider = _provider(session)
        provider.translate(_request(json_mode=True))
        call = session.calls[0]
        assert call["url"] == "https://api.example.com/v1/chat/completions"
        assert call["headers"]["Authorization"] == "Bearer sk-test"
        assert call["json"]["model"] == "gpt-4o-mini"
        assert call["json"]["messages"][0] == {"role": "system", "content": "sys"}
        assert call["json"]["temperature"] == 0.3
        assert call["json"]["response_format"] == {"type": "json_object"}
        assert call["timeout"] == 30

    def test_json_mode_disabled_omits_response_format(self):
        session = FakeSession(FakeResponse(json_data=GOOD_BODY))
        provider = _provider(session)
        provider.translate(_request(json_mode=False))
        assert "response_format" not in session.calls[0]["json"]

    def test_registry_creates_openai_provider(self):
        provider = create_provider("openai", base_url="https://x/v1", api_key="k", model="m")
        assert provider.name == "openai"
        assert set(available_providers()) >= {"openai", "offline"}
        try:
            create_provider("does-not-exist")
        except ProviderConfigError:
            pass
        else:
            raise AssertionError("unknown provider must raise ProviderConfigError")


class TestErrorMapping:
    def test_timeout_is_transient(self):
        session = FakeSession(requests.Timeout("too slow"))
        try:
            _provider(session).translate(_request())
        except ProviderTransientError:
            pass
        else:
            raise AssertionError("timeout must map to ProviderTransientError")

    def test_connection_error_is_transient(self):
        session = FakeSession(requests.ConnectionError("refused"))
        try:
            _provider(session).translate(_request())
        except ProviderTransientError:
            pass
        else:
            raise AssertionError("connection error must map to ProviderTransientError")

    def test_401_is_auth_error(self):
        session = FakeSession(FakeResponse(status_code=401, json_data={"error": {"message": "bad key"}}))
        try:
            _provider(session).translate(_request())
        except ProviderAuthError as exc:
            assert "bad key" in str(exc)
        else:
            raise AssertionError("401 must map to ProviderAuthError")

    def test_429_carries_retry_after(self):
        session = FakeSession(FakeResponse(status_code=429, headers={"Retry-After": "3"}, text="slow down"))
        try:
            _provider(session).translate(_request())
        except ProviderRateLimitError as exc:
            assert exc.retry_after == 3.0
        else:
            raise AssertionError("429 must map to ProviderRateLimitError")

    def test_500_is_transient(self):
        session = FakeSession(FakeResponse(status_code=500, text="boom"))
        try:
            _provider(session).translate(_request())
        except ProviderTransientError:
            pass
        else:
            raise AssertionError("500 must map to ProviderTransientError")

    def test_400_is_config_error(self):
        session = FakeSession(FakeResponse(status_code=400, json_data={"error": {"message": "bad parameter"}}))
        try:
            _provider(session).translate(_request())
        except ProviderConfigError:
            pass
        else:
            raise AssertionError("400 must map to ProviderConfigError")

    def test_400_rejecting_response_format_falls_back_without_json_mode(self):
        reject = FakeResponse(status_code=400, json_data={"error": {"message": "'response_format' is not supported"}})
        ok = FakeResponse(json_data=GOOD_BODY)
        session = FakeSession([reject, ok])
        provider = _provider(session)
        response = provider.translate(_request(json_mode=True))
        assert response.content == "{\"translations\": []}"
        assert len(session.calls) == 2
        assert "response_format" in session.calls[0]["json"]
        assert "response_format" not in session.calls[1]["json"]


class TestBadResponses:
    def test_non_json_body(self):
        session = FakeSession(FakeResponse(status_code=200, text="not json"))
        try:
            _provider(session).translate(_request())
        except ProviderBadResponseError:
            pass
        else:
            raise AssertionError("non-JSON 200 body must map to ProviderBadResponseError")

    def test_no_choices(self):
        session = FakeSession(FakeResponse(status_code=200, json_data={"choices": []}))
        try:
            _provider(session).translate(_request())
        except ProviderBadResponseError:
            pass
        else:
            raise AssertionError("empty choices must map to ProviderBadResponseError")

    def test_empty_content(self):
        body = {"choices": [{"message": {"content": ""}}]}
        session = FakeSession(FakeResponse(status_code=200, json_data=body))
        try:
            _provider(session).translate(_request())
        except ProviderBadResponseError:
            pass
        else:
            raise AssertionError("empty content must map to ProviderBadResponseError")


class TestValidationAndConnection:
    def test_validate_config(self):
        provider = OpenAICompatibleProvider(base_url="", api_key="", model="", session=FakeSession(None))
        problems = provider.validate_config()
        assert any("Base URL" in p for p in problems)
        assert any("Model" in p for p in problems)
        assert any("API key" in p for p in problems)

    def test_translate_without_key_raises_config_error(self):
        session = FakeSession(FakeResponse(json_data=GOOD_BODY))
        provider = OpenAICompatibleProvider(
            base_url="https://api.example.com/v1", api_key="", model="m", session=session,
        )
        try:
            provider.translate(_request())
        except ProviderConfigError as exc:
            assert "API key" in str(exc)
        else:
            raise AssertionError("missing key must fail before any HTTP call")
        assert session.calls == []  # nothing was sent

    def test_test_connection_ok_and_fail(self):
        ok_session = FakeSession(FakeResponse(json_data=GOOD_BODY))
        ok = _provider(ok_session).test_connection()
        assert ok[0] is True
        bad_session = FakeSession(FakeResponse(status_code=401, text="nope"))
        bad = _provider(bad_session).test_connection()
        assert bad[0] is False


class TestOfflineProvider:
    def test_translations_contract(self):
        provider = OfflineDebugProvider()
        request = ProviderRequest(
            model="",
            messages=[],
            payload={"lines": [{"i": 0, "en": "Hello"}, {"i": 1, "en": "World"}]},
        )
        import json as jsonlib

        response = provider.translate(request)
        data = jsonlib.loads(response.content)
        assert data["translations"][0] == {"i": 0, "fa": "[آفلاین] Hello"}
        assert data["translations"][1] == {"i": 1, "fa": "[آفلاین] World"}

    def test_simulated_failures(self):
        provider = OfflineDebugProvider(fail_times=2)
        request = ProviderRequest(model="", messages=[], payload={"lines": []})
        for _ in range(2):
            try:
                provider.translate(request)
            except ProviderTransientError:
                continue
            raise AssertionError("expected simulated transient failure")
        response = provider.translate(request)  # third call succeeds
        assert response.model == "offline-debug"
