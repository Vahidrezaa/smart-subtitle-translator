"""Plain dataclasses mirroring the database rows (no ORM)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GlossaryEntry:
    english_term: str
    persian_primary: str
    persian_alternatives: list[str] = field(default_factory=list)
    explanation: str = ""
    category: str = ""
    notes: str = ""
    id: int | None = None
    created_at: str = ""
    updated_at: str = ""

    def alternatives_for_prompt(self) -> list[str]:
        return [self.persian_primary] + list(self.persian_alternatives)


@dataclass
class MemoryEntry:
    source_text: str
    persian_text: str
    source_norm: str = ""
    source_hash: str = ""
    context_hash: str = ""
    context_summary: list[str] = field(default_factory=list)  # normalized previous lines
    glossary_version: str = ""
    model: str = ""
    status: str = "machine"  # new | machine | corrected | approved
    speaker: str = ""
    source_file: str = ""
    id: int | None = None
    created_at: str = ""
    updated_at: str = ""
    use_count: int = 0


@dataclass
class ProjectRecord:
    name: str
    srt_path: str
    file_hash: str = ""
    target_language: str = "fa"
    model: str = ""
    glossary_version: str = ""
    next_batch: int = 0
    state: str = "new"  # new | in_progress | paused | done | failed
    id: int | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass
class EntryRecord:
    position: int
    sequence: int
    start_ms: int
    end_ms: int
    original_text: str
    working_text: str
    translated_text: str | None = None
    status: str = "untranslated"
    speaker: str = ""
    source: str = "srt"
    correction_id: int | None = None
    id: int | None = None
    project_id: int | None = None


@dataclass
class CorrectionRecord:
    position: int
    original_text: str
    proposed_text: str
    confidence: float = 0.0
    reason: str = ""
    method: str = "heuristic"  # heuristic | llm | manual
    status: str = "proposed"  # proposed | approved | rejected
    project_id: int | None = None
    id: int | None = None
    created_at: str = ""
    decided_at: str | None = None
