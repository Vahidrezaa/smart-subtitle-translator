"""Tests for context window construction and translation engine behavior."""
from __future__ import annotations

import json

import pytest

from app.core.context_manager import ContextManager
from app.core.glossary_manager import GlossaryManager
from app.core.subtitle_model import SubtitleDocument, SubtitleEntry
from app.core.translation_engine import (
    IncompleteTranslationError,
    TranslationEngine,
    parse_translations,
    system_prompt,
)
from app.database.models import GlossaryEntry
from app.providers.base_provider import ProviderBadResponseError

from app.tests.fakes import FakeProvider, ScriptedProvider


def _doc(texts: list[str]) -> SubtitleDocument:
    return SubtitleDocument(
        entries=[
            SubtitleEntry(position=i, sequence=i + 1, start_ms=i * 2000,
                          end_ms=i * 2000 + 1500, original_text=text)
            for i, text in enumerate(texts)
        ]
    )


class TestContextManager:
    def test_window_composition(self):
        doc = _doc([f"line {n}" for n in range(10)])
        manager = ContextManager(prev_lines=2, future_lines=1)
        context = manager.build(doc, [5])
        assert [line.text for line in context.previous] == ["line 3", "line 4"]
        assert [line.position for line in context.lines] == [5]
        assert context.upcoming == ["line 6"]

    def test_window_clamps_at_boundaries(self):
        doc = _doc(["first", "second"])
        manager = ContextManager(prev_lines=3, future_lines=3)
        context = manager.build(doc, [0])
        assert context.previous == []
        assert context.upcoming == ["second"]

    def test_previous_translations_included_when_available(self):
        doc = _doc(["alpha", "beta"])
        doc.get(0).apply_translation("الف", "machine")
        context = ContextManager().build(doc, [1])
        assert context.previous[0].translation == "الف"
        prompt = context.to_prompt_dict()
        assert prompt["previous"][0]["fa"] == "الف"

    def test_correction_note_added(self):
        doc = _doc(["original text here"])
        doc.get(0).working_text = "corrected text here"
        context = ContextManager().build(doc, [0])
        assert any("correction" in note for note in context.notes)

    def test_line_context_hash_stable_and_sensitive(self):
        doc = _doc(["alpha", "beta", "gamma"])
        manager = ContextManager(prev_lines=2)
        hash_a = manager.line_context_hash(doc, 2)
        hash_b = manager.line_context_hash(doc, 2)
        assert hash_a == hash_b  # stable across calls
        doc.get(0).working_text = "changed"
        hash_c = manager.line_context_hash(doc, 2)
        assert hash_a != hash_c  # sensitive to previous-line edits


class TestParseTranslations:
    def test_plain_and_wrapped(self):
        good = json.dumps({"translations": [{"i": 0, "fa": "الف"}, {"i": 1, "fa": "ب"}]})
        assert parse_translations(good, {0, 1}) == {0: "الف", 1: "ب"}
        assert parse_translations(
            json.dumps([{"i": 0, "fa": "الف"}]), {0}
        ) == {0: "الف"}

    def test_markdown_fence_tolerated(self):
        content = "Here you go:\n```json\n{\"translations\": [{\"i\": 3, \"fa\": \"پ\"}]}\n```\nDone."
        assert parse_translations(content, {3}) == {3: "پ"}

    def test_leading_chatter_tolerated(self):
        content = "Sure! {\"translations\": [{\"i\": 0, \"fa\": \"سلام\"}]}"
        assert parse_translations(content, {0}) == {0: "سلام"}

    def test_missing_line_raises_incomplete(self):
        content = json.dumps({"translations": [{"i": 0, "fa": "الف"}]})
        with pytest.raises(IncompleteTranslationError):
            parse_translations(content, {0, 1})

    def test_empty_translation_counts_as_missing(self):
        content = json.dumps({"translations": [{"i": 0, "fa": ""}]})
        with pytest.raises(IncompleteTranslationError):
            parse_translations(content, {0})

    def test_invalid_json_raises(self):
        with pytest.raises(ProviderBadResponseError):
            parse_translations("this is not json at all {", {0})

    def test_wrong_shape_raises(self):
        with pytest.raises(ProviderBadResponseError):
            parse_translations(json.dumps({"unexpected": 1}), {0})


class TestTranslationEngine:
    def _engine(self, provider, db=None, glossary: GlossaryManager | None = None):
        glossary = glossary or (GlossaryManager(db) if db else None)
        if glossary is None:
            raise ValueError("a glossary manager is required")
        return TranslationEngine(provider, glossary, ContextManager(), target_language="fa")

    def test_happy_path(self, db):
        doc = _doc(["Hello world.", "How are you?"])
        engine = self._engine(FakeProvider(), db)
        result = engine.translate_positions(doc, [0, 1])
        assert set(result.results) == {0, 1}
        assert result.results[0].persian == "ترجمه Hello world."
        assert result.results[0].status.value == "machine"
        assert result.model == "fake-model"
        # prompt payload carries context structure
        payload = engine.provider.calls[0].payload
        assert [line["en"] for line in payload["lines"]] == ["Hello world.", "How are you?"]
        assert "previous" in payload and "upcoming" in payload and "glossary" in payload

    def test_system_prompt_contains_glossary_rule(self, db):
        doc = _doc(["Hello."])
        engine = self._engine(FakeProvider(), db)
        engine.translate_positions(doc, [0])
        system = engine.provider.calls[0].messages[0].content
        assert "glossary" in system.lower()
        assert "Persian" in system

    def test_glossary_injected_into_prompt(self, db):
        GlossaryManager(db).add(GlossaryEntry(
            english_term="dependency injection", persian_primary="تزریق وابستگی"))
        glossary = GlossaryManager(db)
        doc = _doc(["We use dependency injection in this project."])
        engine = self._engine(FakeProvider(), glossary=glossary)
        engine.translate_positions(doc, [0])
        payload = engine.provider.calls[0].payload
        assert payload["glossary"] == [{
            "en": "dependency injection",
            "fa": "تزریق وابستگی",
            "alt": [],
            "category": "",
        }]

    def test_glossary_violation_emits_warning(self, db):
        glossary = GlossaryManager(db)
        glossary.add(GlossaryEntry(english_term="dependency injection",
                                   persian_primary="تزریق وابستگی"))
        warnings_seen = []
        provider = ScriptedProvider([
            json.dumps({"translations": [{"i": 0, "fa": "ما از تزریق کار دارند استفاده می‌کنیم"}]})
        ])
        engine = TranslationEngine(provider, glossary, ContextManager(),
                                   listener=lambda e: warnings_seen.append(e))
        doc = _doc(["We use dependency injection here."])
        result = engine.translate_positions(doc, [0])
        assert result.results[0].warnings
        assert any("glossary" in e.message for e in warnings_seen)

    def test_hallucination_guard_warns(self, db):
        provider = ScriptedProvider([
            json.dumps({"translations": [{"i": 0, "fa": "خیلی طولانی " * 30}]})
        ])
        engine = self._engine(provider, db)
        doc = _doc(["Short text."])
        result = engine.translate_positions(doc, [0])
        assert any("hallucination" in w for w in result.results[0].warnings)

    def test_incomplete_response_raises(self, db):
        provider = ScriptedProvider([json.dumps({"translations": [{"i": 0, "fa": "الف"}]})])
        engine = self._engine(provider, db)
        doc = _doc(["one", "two"])
        with pytest.raises(IncompleteTranslationError):
            engine.translate_positions(doc, [0, 1])

    def test_empty_positions_no_call(self, db):
        provider = FakeProvider()
        engine = self._engine(provider, db)
        engine.translate_positions(_doc(["x"]), [])
        assert provider.calls == []
