"""Persistent translation memory with context-aware reuse rules.

A stored translation may only be reused when:
* the source text matches exactly after normalization (hash equality, with a
  difflib double-check against rare hash collisions), and
* the stored context (previous lines) is sufficiently similar to the current
  context, with a stricter threshold for less-trusted statuses
  (machine < corrected < approved), and
* ambiguous lines (short or pronoun-only) require the *exact* same previous
  context — never a fuzzy match.

Approximate matches are never applied automatically.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from app.core.subtitle_model import MemoryStatus
from app.core.textutils import looks_ambiguous, normalize_for_match, stable_hash
from app.database.database_manager import DatabaseManager
from app.database.models import MemoryEntry

log = logging.getLogger(__name__)


@dataclass
class MemoryPolicy:
    reuse_enabled: bool = True
    min_affinity_approved: float = 0.5
    min_affinity_corrected: float = 0.8
    min_affinity_machine: float = 0.9
    require_exact_context_when_ambiguous: bool = True
    verify_similarity: float = 0.98


@dataclass
class MemoryQuery:
    position: int
    source_text: str  # the working text that would be sent for translation
    context_hash: str
    prev_norms: list[str] = field(default_factory=list)
    speaker: str = ""


@dataclass
class ReuseDecision:
    position: int
    reuse: bool
    persian_text: str = ""
    memory_id: int | None = None
    status: MemoryStatus = MemoryStatus.MACHINE
    affinity: float = 0.0
    reason: str = ""


_THRESHOLDS = {
    MemoryStatus.APPROVED: "min_affinity_approved",
    MemoryStatus.CORRECTED: "min_affinity_corrected",
    MemoryStatus.MACHINE: "min_affinity_machine",
}


class TranslationMemory:
    def __init__(self, db: DatabaseManager, policy: MemoryPolicy | None = None) -> None:
        self._db = db
        self.policy = policy or MemoryPolicy()

    # ------------------------------------------------------------- key helpers
    @staticmethod
    def source_hash(text: str) -> str:
        return stable_hash(normalize_for_match(text))

    @staticmethod
    def context_hash_for(source_text: str, prev_texts: list[str]) -> str:
        payload = json.dumps(
            [normalize_for_match(source_text)]
            + [normalize_for_match(t) for t in prev_texts],
            ensure_ascii=False,
        )
        return stable_hash(payload)

    # ------------------------------------------------------------------ lookup
    def find_reusable(self, queries: list[MemoryQuery]) -> list[ReuseDecision]:
        if not queries:
            return []
        if not self.policy.reuse_enabled:
            return [
                ReuseDecision(q.position, False, reason="memory reuse disabled")
                for q in queries
            ]
        by_hash: dict[str, list[MemoryQuery]] = {}
        for query in queries:
            by_hash.setdefault(self.source_hash(query.source_text), []).append(query)
        candidates = self._db.memory_find_by_hashes(list(by_hash))
        return [self._decide(query, candidates) for query in queries]

    def _decide(self, query: MemoryQuery, candidates: list[MemoryEntry]) -> ReuseDecision:
        target_hash = self.source_hash(query.source_text)
        matches = []
        for candidate in candidates:
            if candidate.source_hash != target_hash:
                continue
            similarity = SequenceMatcher(
                None,
                normalize_for_match(candidate.source_text),
                normalize_for_match(query.source_text),
            ).ratio()
            if similarity < self.policy.verify_similarity:
                continue
            if query.speaker and candidate.speaker and query.speaker != candidate.speaker:
                continue
            matches.append(candidate)
        if not matches:
            return ReuseDecision(query.position, False, reason="no memory match")

        best = max(matches, key=lambda c: self._affinity(c, query))
        affinity = self._affinity(best, query)
        try:
            status = MemoryStatus(best.status)
        except ValueError:
            status = MemoryStatus.MACHINE
        threshold = getattr(self.policy, _THRESHOLDS[status])

        if (
            self.policy.require_exact_context_when_ambiguous
            and looks_ambiguous(query.source_text)
            and best.context_hash != query.context_hash
        ):
            return ReuseDecision(
                query.position,
                False,
                affinity=affinity,
                status=status,
                reason="ambiguous line requires the exact same context",
            )
        if affinity < threshold:
            return ReuseDecision(
                query.position,
                False,
                affinity=affinity,
                status=status,
                reason=(
                    f"context affinity {affinity:.2f} below required {threshold:.2f} "
                    f"for {status.value} memory"
                ),
            )
        return ReuseDecision(
            query.position,
            True,
            persian_text=best.persian_text,
            memory_id=best.id,
            status=status,
            affinity=affinity,
            reason=f"reused {status.value} memory (affinity {affinity:.2f})",
        )

    @staticmethod
    def _affinity(candidate: MemoryEntry, query: MemoryQuery) -> float:
        if candidate.context_hash and query.context_hash and candidate.context_hash == query.context_hash:
            return 1.0
        stored = candidate.context_summary
        if isinstance(stored, str):
            try:
                stored = json.loads(stored or "[]")
            except ValueError:
                stored = []
        if not isinstance(stored, list):
            stored = []
        stored_text = " ".join(str(item) for item in stored)
        current_text = " ".join(query.prev_norms)
        if not stored_text and not current_text:
            return 1.0  # both contexts empty (e.g. first lines) — identical
        if not stored_text or not current_text:
            return 0.0
        return SequenceMatcher(None, stored_text, current_text).ratio()

    # ------------------------------------------------------------------ record
    def record(
        self,
        source_text: str,
        persian_text: str,
        prev_texts: list[str],
        glossary_version: str = "",
        model: str = "",
        status: MemoryStatus = MemoryStatus.MACHINE,
        speaker: str = "",
        source_file: str = "",
    ) -> int:
        return self._db.memory_put(
            MemoryEntry(
                source_text=source_text,
                persian_text=persian_text,
                source_norm=normalize_for_match(source_text),
                source_hash=self.source_hash(source_text),
                context_hash=self.context_hash_for(source_text, prev_texts),
                context_summary=[normalize_for_match(t) for t in prev_texts],
                glossary_version=glossary_version,
                model=model,
                status=status.value,
                speaker=speaker,
                source_file=source_file,
            )
        )
