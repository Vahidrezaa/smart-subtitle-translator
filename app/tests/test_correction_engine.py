"""Tests for ASR-error detection heuristics, LLM review and the approve/reject flow."""
from __future__ import annotations

import json

import pytest

from app.core.correction_engine import CorrectionEngine, CorrectionProposal
from app.core.exceptions import ConfigurationError
from app.core.glossary_manager import GlossaryManager
from app.core.subtitle_model import SubtitleDocument, SubtitleEntry
from app.database.models import CorrectionRecord, GlossaryEntry, ProjectRecord

from app.tests.fakes import FakeProvider, ScriptedProvider


def _project(db, name: str = "demo") -> int:
    return db.project_create(ProjectRecord(name=name, srt_path=f"{name}.srt"))


def _doc(texts: list[str]) -> SubtitleDocument:
    return SubtitleDocument(
        entries=[
            SubtitleEntry(position=i, sequence=i + 1, start_ms=i * 2000,
                          end_ms=i * 2000 + 1500, original_text=text)
            for i, text in enumerate(texts)
        ]
    )


class TestHeuristics:
    def test_stutter_detection(self, db):
        doc = _doc(["Today the the we talk about testing.", "Fine line here."])
        engine = CorrectionEngine(db, GlossaryManager(db))
        proposals = engine.scan_heuristics(doc, project_id=1, store=False)
        assert len(proposals) == 1
        assert proposals[0].position == 0
        assert proposals[0].proposed_text == "Today the we talk about testing."

    def test_triple_repeat_detected_even_for_content_words(self, db):
        doc = _doc(["It was very very very good."])
        engine = CorrectionEngine(db, GlossaryManager(db))
        proposals = engine.scan_heuristics(doc, project_id=1, store=False)
        assert proposals[0].proposed_text == "It was very good."

    def test_legitimate_double_not_flagged(self, db):
        doc = _doc(["That had had surprising results.", "A normal sentence follows."])
        engine = CorrectionEngine(db, GlossaryManager(db))
        proposals = engine.scan_heuristics(doc, project_id=1, store=False)
        assert proposals == []

    def test_glossary_distance_detection(self, db):
        glossary = GlossaryManager(db)
        glossary.add(GlossaryEntry(english_term="dependency injection",
                                   persian_primary="تزریق وابستگی"))
        doc = _doc(["We rely on dependency injecton everywhere."])
        engine = CorrectionEngine(db, glossary)
        proposals = engine.scan_heuristics(doc, project_id=1, store=False)
        assert len(proposals) == 1
        assert "dependency injection" in proposals[0].proposed_text
        assert proposals[0].confidence >= 0.6
        assert "edit distance" in proposals[0].reason
    def test_echo_detection(self, db):
        doc = _doc(["Welcome to the show.", "Welcome to the show.", "Next topic."])
        engine = CorrectionEngine(db, GlossaryManager(db))
        proposals = engine.scan_heuristics(doc, project_id=1, store=False)
        assert len(proposals) == 1
        assert proposals[0].position == 1
        assert proposals[0].proposed_text == proposals[0].original_text  # flag only

    def test_proposals_stored_in_database(self, db):
        project_id = _project(db, "store")
        doc = _doc(["This is is a problem."])
        engine = CorrectionEngine(db, GlossaryManager(db))
        engine.scan_heuristics(doc, project_id=project_id, store=True)
        pending = db.correction_list(project_id, status="proposed")
        assert len(pending) == 1
        assert pending[0].method == "heuristic"
        assert pending[0].position == 0


class TestLLMReview:
    def test_review_requires_provider(self, db):
        doc = _doc(["Hello."])
        engine = CorrectionEngine(db, GlossaryManager(db))
        with pytest.raises(ConfigurationError):
            engine.review_with_llm(doc, project_id=1)

    def test_review_stores_suspected_lines(self, db):
        provider = ScriptedProvider([
            json.dumps({"reviews": [
                {"i": 1, "suspect": True, "correction": "dependency injection",
                 "confidence": 0.9, "reason": "ASR mis-heard 'injection'"},
                {"i": 0, "suspect": False, "correction": "Welcome.", "confidence": 0.9,
                 "reason": "fine"},
                {"i": 2, "suspect": True, "correction": "next topic",
                 "confidence": 0.1, "reason": "unsure"},  # below min confidence
            ]})
        ])
        doc = _doc(["Welcome.", "We use dependency injecton here.", "Next topic."])
        project_id = _project(db, "review")
        engine = CorrectionEngine(db, GlossaryManager(db), provider=provider)
        proposals = engine.review_with_llm(doc, project_id=project_id)
        assert len(proposals) == 1
        assert proposals[0].position == 1
        assert proposals[0].method == "llm"
        stored = db.correction_list(project_id, status="proposed")
        assert len(stored) == 1
        assert stored[0].proposed_text == "dependency injection"

    def test_review_payload_shape(self, db):
        provider = FakeProvider()
        doc = _doc(["Hello world.", "Second line."])
        project_id = _project(db, "payload")
        engine = CorrectionEngine(db, GlossaryManager(db), provider=provider)
        engine.review_with_llm(doc, project_id=project_id)
        payload = provider.calls[0].payload
        assert {"previous", "captions", "glossary"} <= set(payload)
        assert [cap["i"] for cap in payload["captions"]] == [0, 1]

    def test_invalid_review_json_is_ignored(self, db):
        provider = ScriptedProvider(["definitely not json"])
        doc = _doc(["Some line."])
        project_id = _project(db, "badjson")
        engine = CorrectionEngine(db, GlossaryManager(db), provider=provider)
        assert engine.review_with_llm(doc, project_id=project_id) == []


class TestApproveReject:
    def _setup(self, db):
        project_id = _project(db, "approve")
        doc = _doc(["We use dependency injecton today."])
        db.entries_replace(project_id, doc.to_records(project_id))
        db.correction_add(
            CorrectionRecord(
                project_id=project_id, position=0,
                original_text=doc.get(0).original_text,
                proposed_text="We use dependency injection today.",
                confidence=0.8, reason="glossary distance", method="heuristic",
            )
        )
        engine = CorrectionEngine(db, GlossaryManager(db))
        return doc, engine, project_id, db.correction_list(project_id, status="proposed")[0].id

    def test_approve_updates_working_text_and_keeps_original(self, db):
        doc, engine, project_id, correction_id = self._setup(db)
        engine.approve(doc, correction_id)
        entry = doc.get(0)
        assert entry.working_text == "We use dependency injection today."
        assert entry.original_text == "We use dependency injecton today."  # preserved
        assert entry.correction_id == correction_id
        assert db.correction_get(correction_id).status == "approved"

    def test_reject_restores_original(self, db):
        doc, engine, project_id, correction_id = self._setup(db)
        engine.approve(doc, correction_id)
        engine.reject(doc, correction_id)
        entry = doc.get(0)
        assert entry.working_text == entry.original_text
        assert entry.correction_id is None
        assert db.correction_get(correction_id).status == "rejected"

    def test_reject_without_prior_approve_is_harmless(self, db):
        doc, engine, project_id, correction_id = self._setup(db)
        engine.reject(doc, correction_id)
        assert doc.get(0).working_text == doc.get(0).original_text

    def test_persistence_roundtrip(self, db):
        doc, engine, project_id, correction_id = self._setup(db)
        engine.approve(doc, correction_id)
        db.entry_set_working_text(project_id, 0, doc.get(0).working_text, doc.get(0).correction_id)
        records = db.entries_load(project_id)
        assert records[0].working_text == "We use dependency injection today."
        assert records[0].correction_id == correction_id
