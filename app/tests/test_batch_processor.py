"""Tests for batching, memory reuse during runs, retries, cancellation and resume."""
from __future__ import annotations

import threading

import pytest

from app.core.batch_processor import BatchProcessor, RunSummary
from app.core.context_manager import ContextManager
from app.core.exceptions import BatchCancelled
from app.core.glossary_manager import GlossaryManager
from app.core.subtitle_model import EntryStatus, SubtitleDocument, SubtitleEntry
from app.core.translation_engine import TranslationEngine
from app.core.translation_memory import MemoryPolicy, TranslationMemory
from app.database.models import ProjectRecord
from app.providers.base_provider import ProviderTransientError

from app.tests.fakes import FakeProvider


def _doc(texts: list[str]) -> SubtitleDocument:
    return SubtitleDocument(
        entries=[
            SubtitleEntry(position=i, sequence=i + 1, start_ms=i * 2000,
                          end_ms=i * 2000 + 1500, original_text=text)
            for i, text in enumerate(texts)
        ]
    )


class TestPlan:
    def test_split_by_max_lines(self):
        doc = _doc([f"line {n}" for n in range(25)])
        processor = BatchProcessor(None, None, None, None, None, max_lines=10, max_chars=100000)
        plan = processor.plan(doc)
        assert [len(b) for b in plan.batches] == [10, 10, 5]

    def test_split_by_max_chars(self):
        doc = _doc(["x" * 100, "y" * 100, "z" * 100])
        processor = BatchProcessor(None, None, None, None, None, max_lines=100, max_chars=250)
        plan = processor.plan(doc)
        assert [len(b) for b in plan.batches] == [2, 1]

    def test_gap_starts_new_batch(self):
        doc = _doc([f"line {n}" for n in range(10)])
        doc.get(3).status = EntryStatus.MACHINE  # already translated
        doc.get(4).status = EntryStatus.MACHINE
        processor = BatchProcessor(None, None, None, None, None, max_lines=100, max_chars=100000)
        plan = processor.plan(doc)
        # positions 0,1,2 then gap of 2 (<=4 so still one batch), then 5..9
        assert plan.batches == [[0, 1, 2, 5, 6, 7, 8, 9]]

    def test_large_gap_splits_batches(self):
        doc = _doc([f"line {n}" for n in range(10)])
        for pos in range(3, 9):  # gap of 6 between position 2 and 9
            doc.get(pos).status = EntryStatus.MACHINE
        processor = BatchProcessor(None, None, None, None, None, max_lines=100, max_chars=100000)
        plan = processor.plan(doc)
        assert plan.batches == [[0, 1, 2], [9]]

    def test_oversized_line_gets_own_batch(self):
        doc = _doc(["word", "z" * 5000, "word"])
        processor = BatchProcessor(None, None, None, None, None, max_lines=100, max_chars=100)
        plan = processor.plan(doc)
        assert [len(b) for b in plan.batches] == [1, 1, 1]


class TestRun:
    def _processor(self, db, provider, doc, **overrides):
        glossary = GlossaryManager(db)
        memory = TranslationMemory(db, MemoryPolicy())
        context = ContextManager()
        engine = TranslationEngine(provider, glossary, context)
        settings = dict(
            db=db, engine=engine, memory=memory, context_manager=context, glossary=glossary,
            max_lines=10, max_chars=100000, max_retries=3, retry_base_delay=0.01,
            source_file="test.srt",
        )
        settings.update(overrides)
        return BatchProcessor(**settings), glossary, memory, context, engine

    def test_full_run_translates_and_persists(self, db):
        project_id = db.project_create(ProjectRecord(name="p", srt_path="p.srt"))
        doc = _doc([f"Sentence number {n} for the test run." for n in range(12)])
        db.entries_replace(project_id, doc.to_records(project_id))
        provider = FakeProvider()
        processor, *_ = self._processor(db, provider, doc, max_lines=5)
        summary = processor.run(project_id, doc)
        assert summary.ok
        assert summary.translated == 12
        assert summary.batches_run == 3
        assert len(provider.calls) == 3  # one request per batch, never the whole file
        assert doc.get(0).translated_text == "ترجمه Sentence number 0 for the test run."
        assert doc.get(0).status is EntryStatus.MACHINE
        assert db.entry_count(project_id, status="machine") == 12
        assert db.project_get(project_id).state == "done"
        assert db.memory_count() == 12

    def test_previous_batch_translations_feed_context(self, db):
        project_id = db.project_create(ProjectRecord(name="p", srt_path="p.srt"))
        doc = _doc([f"Common sentence {n} with enough words." for n in range(4)])
        db.entries_replace(project_id, doc.to_records(project_id))
        provider = FakeProvider()
        processor, *_ = self._processor(db, provider, doc, max_lines=2)
        processor.run(project_id, doc)
        # second batch request must include the first batch's Persian in "previous"
        second_payload = provider.calls[1].payload
        assert second_payload["previous"], "second batch must see previous lines"
        assert any(prev.get("fa") for prev in second_payload["previous"])

    def test_cancellation_resumes_without_duplicate_requests(self, db):
        project_id = db.project_create(ProjectRecord(name="p", srt_path="p.srt"))
        doc = _doc([f"Long enough sentence {n} to be unambiguous." for n in range(15)])
        db.entries_replace(project_id, doc.to_records(project_id))
        provider = FakeProvider()
        processor, *_ = self._processor(db, provider, doc, max_lines=5)

        from app.core.events import BatchFinished, ProgressEvent

        cancel_after_first = {"flag": False}

        def listener(event: ProgressEvent) -> None:
            if isinstance(event, BatchFinished) and event.batch_index == 0:
                cancel_after_first["flag"] = True

        def cancel_check() -> bool:
            return cancel_after_first["flag"]

        processor, *_ = self._processor(db, provider, doc, max_lines=5,
                                        listener=listener, cancel_check=cancel_check)

        with pytest.raises(BatchCancelled):
            processor.run(project_id, doc)
        assert len(provider.calls) == 1  # only batch 1 was sent
        assert db.project_get(project_id).state == "paused"

        # resume in a NEW processor (simulates app restart): state comes from DB
        doc2 = SubtitleDocument.from_records(db.entries_load(project_id))
        provider2 = FakeProvider()
        processor2, *_ = self._processor(db, provider2, doc2, max_lines=5)
        summary = processor2.run(project_id, doc2)
        assert summary.ok
        assert summary.translated == 10  # remaining two batches
        assert len(provider2.calls) == 2  # batches 2 and 3 only — no duplicates

    def test_memory_prevents_duplicate_api_calls_on_second_file(self, db):
        project_id = db.project_create(ProjectRecord(name="p1", srt_path="one.srt"))
        doc = _doc([f"Repeated unique sentence {n} for memory." for n in range(6)])
        db.entries_replace(project_id, doc.to_records(project_id))
        provider = FakeProvider()
        processor, *_ = self._processor(db, provider, doc)
        processor.run(project_id, doc)
        assert len(provider.calls) == 1

        # a fresh project with the same content must reuse everything
        project2 = db.project_create(ProjectRecord(name="p2", srt_path="two.srt"))
        doc2 = _doc([f"Repeated unique sentence {n} for memory." for n in range(6)])
        db.entries_replace(project2, doc2.to_records(project2))
        provider2 = FakeProvider()
        processor2, *_ = self._processor(db, provider2, doc2)
        summary = processor2.run(project2, doc2)
        assert summary.reused == 6
        assert summary.translated == 0
        assert provider2.calls == []  # zero API calls

    def test_retries_then_permanent_failure_marks_lines_failed(self, db):
        project_id = db.project_create(ProjectRecord(name="p", srt_path="p.srt"))
        doc = _doc([f"Sentence {n} for the failure test." for n in range(6)])
        db.entries_replace(project_id, doc.to_records(project_id))
        provider = FakeProvider(fail_times=99)  # always fails
        processor, *_ = self._processor(db, provider, doc, max_lines=10, max_retries=2,
                                        retry_base_delay=0.01)
        summary = processor.run(project_id, doc)
        assert not summary.ok
        assert summary.failed == 6
        assert all(doc.get(n).status is EntryStatus.FAILED for n in range(6))
        assert db.entry_count(project_id, status="failed") == 6
        assert db.project_get(project_id).state == "failed"

    def test_retranslate_failed_recovers(self, db):
        project_id = db.project_create(ProjectRecord(name="p", srt_path="p.srt"))
        doc = _doc([f"Sentence {n} for recovery." for n in range(6)])
        db.entries_replace(project_id, doc.to_records(project_id))
        failing = FakeProvider(fail_times=99)
        processor, *_ = self._processor(db, failing, doc, max_lines=10, max_retries=1,
                                        retry_base_delay=0.01)
        processor.run(project_id, doc)
        working = FakeProvider()
        processor2, *_ = self._processor(db, working, doc, max_lines=10)
        summary = processor2.retranslate_failed(project_id, doc)
        assert summary.ok
        assert summary.translated == 6
        assert all(doc.get(n).status is EntryStatus.MACHINE for n in range(6))
        assert db.project_get(project_id).state == "done"

    def test_cancel_mid_batch_persists_paused_state(self, db):
        project_id = db.project_create(ProjectRecord(name="p", srt_path="p.srt"))
        doc = _doc([f"Cancel mid batch sentence {n}." for n in range(5)])
        db.entries_replace(project_id, doc.to_records(project_id))
        provider = FakeProvider()
        processor, *_ = self._processor(db, provider, doc, max_lines=10)
        processor._cancel_event = threading.Event()
        processor._cancel_event.set()
        with pytest.raises(BatchCancelled):
            processor.run(project_id, doc)
        assert db.project_get(project_id).state == "paused"
        assert provider.calls == []  # nothing was sent

    def test_progress_events_flow(self, db):
        project_id = db.project_create(ProjectRecord(name="p", srt_path="p.srt"))
        doc = _doc([f"Progress event sentence {n}." for n in range(4)])
        db.entries_replace(project_id, doc.to_records(project_id))
        provider = FakeProvider()
        processor, *_ = self._processor(db, provider, doc, max_lines=10)
        seen: list[str] = []
        processor._listener = lambda e: seen.append(type(e).__name__)
        processor.run(project_id, doc)
        assert "BatchStarted" in seen and "LineTranslated" in seen
        assert "BatchFinished" in seen and "RunFinished" in seen


def test_run_summary_defaults():
    summary = RunSummary()
    assert summary.ok and summary.translated == 0 and summary.failed_positions == []
