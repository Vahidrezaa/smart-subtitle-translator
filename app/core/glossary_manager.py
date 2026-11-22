"""Glossary facade: CRUD over the database plus term detection in subtitle text.

Every write invalidates the term cache and bumps ``version()`` — a content
signature stamped into prompts and translation-memory rows so any translation
is traceable to the glossary state that produced it.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.core.textutils import stable_hash
from app.database.database_manager import DatabaseManager
from app.database.models import GlossaryEntry

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GlossaryHit:
    entry: GlossaryEntry
    start: int
    end: int
    matched_text: str


class GlossaryManager:
    def __init__(self, db: DatabaseManager) -> None:
        self._db = db
        self._patterns: dict[str, re.Pattern] = {}
        self._term_cache: list[GlossaryEntry] | None = None
        self._version: str | None = None

    # ------------------------------------------------------------------- CRUD
    def add(self, entry: GlossaryEntry) -> int:
        entry_id = self._db.glossary_add(entry)
        self._invalidate()
        return entry_id

    def update(self, entry_id: int, **changes) -> None:
        self._db.glossary_update(entry_id, **changes)
        self._invalidate()

    def delete(self, entry_id: int) -> None:
        self._db.glossary_delete(entry_id)
        self._invalidate()

    def get(self, entry_id: int) -> GlossaryEntry | None:
        return self._db.glossary_get(entry_id)

    def list_entries(self, search: str = "", category: str = "") -> list[GlossaryEntry]:
        return self._db.glossary_list(search=search, category=category)

    def categories(self) -> list[str]:
        return self._db.glossary_categories()

    def count(self) -> int:
        return self._db.glossary_count()

    # --------------------------------------------------------------- matching
    def all_terms(self, refresh: bool = False) -> list[GlossaryEntry]:
        if self._term_cache is None or refresh:
            self._term_cache = self._db.glossary_list()
        return self._term_cache

    def version(self) -> str:
        if self._version is None:
            terms = self.all_terms(refresh=True)
            # Signature covers ALL content fields, not just updated_at (which has
            # second resolution — two edits within one second must still differ).
            payload = "|".join(
                "{}:{}:{}:{}:{}:{}".format(
                    t.id,
                    t.english_term.lower(),
                    t.persian_primary,
                    ",".join(t.persian_alternatives),
                    t.category,
                    t.notes,
                )
                for t in sorted(terms, key=lambda x: x.id or 0)
            )
            self._version = f"v{len(terms)}:{stable_hash(payload)[:8]}"
        return self._version

    def _invalidate(self) -> None:
        self._term_cache = None
        self._version = None
        self._patterns = {}

    @staticmethod
    def _term_pattern(term: str) -> re.Pattern:
        # Lookarounds instead of \b so terms like "C++" or ".NET" work.
        return re.compile(rf"(?<![\w]){re.escape(term)}(?![\w])", re.IGNORECASE)

    def find_terms(self, text: str) -> list[GlossaryHit]:
        """Find glossary terms in ``text``. Longest terms win; overlapping
        matches are dropped (e.g. "Python 3" beats "Python" when both exist)."""
        if not text:
            return []
        hits: list[GlossaryHit] = []
        taken: list[tuple[int, int]] = []
        entries = sorted(self.all_terms(), key=lambda e: len(e.english_term), reverse=True)
        for entry in entries:
            key = entry.english_term.lower()
            pattern = self._patterns.get(key)
            if pattern is None:
                pattern = self._term_pattern(entry.english_term)
                self._patterns[key] = pattern
            match = pattern.search(text)
            if not match:
                continue
            span = (match.start(), match.end())
            if any(not (span[1] <= start or span[0] >= end) for start, end in taken):
                continue
            taken.append(span)
            hits.append(
                GlossaryHit(entry=entry, start=span[0], end=span[1], matched_text=match.group(0))
            )
        return sorted(hits, key=lambda h: h.start)
