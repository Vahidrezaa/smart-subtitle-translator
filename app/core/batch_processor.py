"""Batch processing: planning, memory reuse, retries, checkpoints, resume.

Guarantees:
* subtitles are sent in manageable batches (max lines / max chars), never the
  whole file in one request;
* contextual continuity — batches are contiguous-ish and every request carries
  the sliding context window, including previous batches' Persian translations;
* progress is checkpointed to SQLite after every batch, so an interrupted run
  resumes from the database (statuses + TM) with no duplicate API requests;
* transient provider errors are retried with exponential backoff; permanent
  ones fail the batch and mark its lines FAILED for a later retry pass.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from app.core.context_manager import ContextManager
from app.core.exceptions import BatchCancelled
from app.core.events import (
    BatchFinished,
    BatchStarted,
    LineTranslated,
    ProgressEvent,
    RetryScheduled,
    RunFinished,
    WarningEvent,
)
from app.core.glossary_manager import GlossaryManager
from app.core.subtitle_model import EntryStatus, SubtitleDocument
from app.core.textutils import normalize_for_match
from app.core.translation_engine import TranslationEngine
from app.core.translation_memory import MemoryQuery, MemoryStatus, ReuseDecision, TranslationMemory
from app.database.database_manager import DatabaseManager
from app.providers.base_provider import (
    ProviderBadResponseError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTransientError,
)

log = logging.getLogger(__name__)

MAX_GAP_BETWEEN_BATCHES = 4  # an index gap larger than this starts a new batch


@dataclass
class BatchPlan:
    batches: list[list[int]]

    def __len__(self) -> int:
        return len(self.batches)


@dataclass
class RunSummary:
    batches_run: int = 0
    translated: int = 0
    reused: int = 0
    failed: int = 0
    failed_positions: list[int] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    ok: bool = True


class BatchProcessor:
    def __init__(
        self,
        db: DatabaseManager,
        engine: TranslationEngine,
        memory: TranslationMemory,
        context_manager: ContextManager,
        glossary: GlossaryManager,
        max_lines: int = 15,
        max_chars: int = 3500,
        max_retries: int = 4,
        retry_base_delay: float = 2.0,
        model: str = "",
        source_file: str = "",
        listener: Callable[[ProgressEvent], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        self._db = db
        self._engine = engine
        self._memory = memory
        self._context = context_manager
        self._glossary = glossary
        self.max_lines = max(1, max_lines)
        self.max_chars = max(1, max_chars)
        self.max_retries = max(0, max_retries)
        self.retry_base_delay = max(0.01, retry_base_delay)
        self.model = model
        self.source_file = source_file
        self._listener = listener
        self._cancel_check = cancel_check or (lambda: False)
        self._cancel_event: threading.Event | None = None
        self._done = 0
        self._total = 0

    # ------------------------------------------------------------------ events
    def _emit(self, event: ProgressEvent) -> None:
        event.done = self._done
        event.total = self._total
        if self._listener:
            try:
                self._listener(event)
            except Exception:
                log.exception("Progress listener raised")

    def _cancelled(self) -> bool:
        if self._cancel_event is not None and self._cancel_event.is_set():
            return True
        return bool(self._cancel_check())

    # ------------------------------------------------------------------- plan
    def plan(self, doc: SubtitleDocument) -> BatchPlan:
        """Group untranslated positions into batches: contiguous-ish runs cut by
        line count, character budget or index gaps. An oversized single line
        still gets its own batch (never dropped, never truncated)."""
        batches: list[list[int]] = []
        current: list[int] = []
        current_chars = 0
        last_position: int | None = None
        for position in doc.untranslated_positions():
            entry = doc.get(position)
            entry_chars = max(1, len(entry.working_text))
            starts_new = (
                (current and len(current) >= self.max_lines)
                or (current and current_chars + entry_chars > self.max_chars)
                or (last_position is not None and position - last_position > MAX_GAP_BETWEEN_BATCHES)
            )
            if starts_new:
                batches.append(current)
                current, current_chars = [], 0
            current.append(position)
            current_chars += entry_chars
            last_position = position
        if current:
            batches.append(current)
        return BatchPlan(batches)

    # -------------------------------------------------------------------- run
    def run(self, project_id: int, doc: SubtitleDocument, start_batch: int = 0) -> RunSummary:
        started = time.monotonic()
        plan = self.plan(doc)
        summary = RunSummary()
        self._total = sum(len(batch) for batch in plan.batches)
        self._done = 0

        for batch_index in range(start_batch, len(plan.batches)):
            positions = plan.batches[batch_index]
            if self._cancelled():
                self._save_progress(project_id, batch_index, "paused")
                self._emit(WarningEvent(message=f"Cancelled before batch {batch_index + 1}; progress saved"))
                summary.ok = False
                raise BatchCancelled(f"Cancelled before batch {batch_index + 1}/{len(plan)}")

            self._emit(BatchStarted(
                batch_index=batch_index, batch_count=len(plan),
                message=f"Batch {batch_index + 1}/{len(plan)} ({len(positions)} lines)",
            ))
            try:
                self._process_batch(project_id, doc, positions, summary)
                summary.batches_run += 1
                self._save_progress(project_id, batch_index + 1, "in_progress")
                self._emit(BatchFinished(
                    batch_index=batch_index,
                    message=f"Batch {batch_index + 1} finished",
                ))
            except BatchCancelled:
                self._save_progress(project_id, batch_index, "paused")
                summary.ok = False
                raise
            except ProviderError as exc:
                summary.failed += len(positions)
                summary.failed_positions.extend(positions)
                for position in positions:
                    self._mark_failed(project_id, doc, position)
                self._save_progress(project_id, batch_index + 1, "in_progress")
                self._emit(WarningEvent(
                    message=f"Batch {batch_index + 1} failed after retries: {exc}",
                ))

        state = "failed" if summary.failed_positions else "done"
        summary.ok = not bool(summary.failed_positions)
        self._save_progress(project_id, len(plan), state)
        summary.elapsed_seconds = time.monotonic() - started
        self._emit(RunFinished(
            ok=summary.ok and not summary.failed_positions,
            message=(
                f"Finished: {summary.translated} translated, {summary.reused} reused, "
                f"{summary.failed} failed"
            ),
        ))
        return summary

    # ----------------------------------------------------------- batch steps
    def _process_batch(
        self, project_id: int, doc: SubtitleDocument, positions: list[int], summary: RunSummary
    ) -> None:
        todo = self._apply_memory_reuse(project_id, doc, positions, summary)
        if todo:
            result = self._call_with_retries(doc, todo)
            glossary_version = self._glossary.version()
            for position in todo:
                line = result.results[position]
                entry = doc.get(position)
                entry.apply_translation(line.persian, EntryStatus.MACHINE)
                self._db.entry_set_translation(project_id, position, line.persian, EntryStatus.MACHINE.value)
                self._memory.record(
                    source_text=entry.working_text,
                    persian_text=line.persian,
                    prev_texts=self._context.prev_texts(doc, position),
                    glossary_version=glossary_version,
                    model=result.model or self.model,
                    status=MemoryStatus.MACHINE,
                    speaker=entry.speaker,
                    source_file=self.source_file,
                )
                summary.translated += 1
                self._done += 1
                self._emit(LineTranslated(position=position, reused=False,
                                          status=EntryStatus.MACHINE.value,
                                          message=f"Line {position} translated"))
        else:
            self._done += len(positions)

    def _apply_memory_reuse(
        self, project_id: int, doc: SubtitleDocument, positions: list[int], summary: RunSummary
    ) -> list[int]:
        if not self._memory.policy.reuse_enabled:
            return list(positions)
        queries = []
        for position in positions:
            entry = doc.get(position)
            source_hash, context_hash = self._context.line_context_hash(doc, position)
            queries.append(MemoryQuery(
                position=position,
                source_text=entry.working_text,
                context_hash=context_hash,
                prev_norms=[normalize_for_match(t) for t in self._context.prev_texts(doc, position)],
                speaker=entry.speaker,
            ))
        todo = list(positions)
        for decision in self._memory.find_reusable(queries):
            if decision.reuse:
                self._apply_reuse(project_id, doc, decision)
                summary.reused += 1
                todo.remove(decision.position)
        return todo

    def _apply_reuse(
        self, project_id: int, doc: SubtitleDocument, decision: ReuseDecision
    ) -> None:
        entry = doc.get(decision.position)
        entry.apply_translation(decision.persian_text, EntryStatus(decision.status.value))
        self._db.entry_set_translation(project_id, decision.position,
                                       decision.persian_text, entry.status.value)
        if decision.memory_id:
            self._db.memory_touch(decision.memory_id)
        self._done += 1
        log.debug("Line %d reused memory: %s", decision.position, decision.reason)
        self._emit(LineTranslated(position=decision.position, reused=True,
                                  status=entry.status.value,
                                  message=f"Line {decision.position} reused from memory"))

    def _call_with_retries(self, doc: SubtitleDocument, positions: list[int]):
        attempt = 0
        while True:
            if self._cancelled():
                raise BatchCancelled()
            try:
                return self._engine.translate_positions(
                    doc, positions, model=self.model,
                )
            except ProviderRateLimitError as exc:
                delay = exc.retry_after or self._backoff(attempt)
                self._retry_or_raise(attempt, exc)
                attempt += 1
                self._emit(RetryScheduled(attempt=attempt, delay_seconds=delay,
                                          message=f"Rate limited; retrying in {delay:.0f}s"))
                time.sleep(delay)
            except (ProviderTransientError, ProviderBadResponseError) as exc:
                delay = self._backoff(attempt)
                self._retry_or_raise(attempt, exc)
                attempt += 1
                self._emit(RetryScheduled(attempt=attempt, delay_seconds=delay,
                                          message=f"Temporary error ({exc}); retrying in {delay:.1f}s"))
                time.sleep(delay)

    def _retry_or_raise(self, attempt: int, exc: ProviderError) -> None:
        if attempt >= self.max_retries:
            raise exc

    def _backoff(self, attempt: int) -> float:
        return min(60.0, self.retry_base_delay * (2 ** attempt))

    # ------------------------------------------------------------- persistence
    def _mark_failed(self, project_id: int, doc: SubtitleDocument, position: int) -> None:
        doc.get(position).status = EntryStatus.FAILED
        self._db.entry_set_translation(project_id, position, None, EntryStatus.FAILED.value)

    def _save_progress(self, project_id: int, next_batch: int, state: str) -> None:
        self._db.project_update(project_id, next_batch=next_batch, state=state)

    # ----------------------------------------------------------- failed retry
    def retranslate_failed(self, project_id: int, doc: SubtitleDocument) -> RunSummary:
        """Reset FAILED lines and run them again (used by the GUI 'retry failed' action)."""
        failed = [e.position for e in doc.entries if e.status is EntryStatus.FAILED]
        if not failed:
            return RunSummary()
        for position in failed:
            doc.get(position).status = EntryStatus.UNTRANSLATED
            self._db.entry_set_translation(project_id, position, None, EntryStatus.UNTRANSLATED.value)
        return self.run(project_id, doc, start_batch=0)
