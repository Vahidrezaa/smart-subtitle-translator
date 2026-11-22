"""Subtitle data model: entries, documents, statuses and SRT timestamp formatting."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from app.database.models import EntryRecord


class EntryStatus(str, Enum):
    UNTRANSLATED = "untranslated"
    QUEUED = "queued"
    TRANSLATING = "translating"
    MACHINE = "machine"
    CORRECTED = "corrected"
    APPROVED = "approved"
    FAILED = "failed"
    SKIPPED = "skipped"


class MemoryStatus(str, Enum):
    NEW = "new"
    MACHINE = "machine"
    CORRECTED = "corrected"
    APPROVED = "approved"


def ms_to_srt_time(ms: int) -> str:
    """Render milliseconds as canonical SRT time: HH:MM:SS,mmm."""
    ms = max(0, int(ms))
    hours, remainder = divmod(ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


@dataclass
class SubtitleEntry:
    """One subtitle cue.

    ``original_text`` is exactly what was parsed from the file and is never
    modified; ``working_text`` is what gets translated (original + approved
    ASR corrections).
    """

    position: int  # 0-based order within the document
    sequence: int  # original SRT sequence number
    start_ms: int
    end_ms: int
    original_text: str
    working_text: str = ""
    translated_text: str | None = None
    status: EntryStatus = EntryStatus.UNTRANSLATED
    speaker: str = ""  # reserved for future speaker identification
    source: str = "srt"  # srt | ocr | game | ... (future)
    correction_id: int | None = None

    def __post_init__(self) -> None:
        if not self.working_text:
            self.working_text = self.original_text

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def apply_translation(self, persian: str, status: EntryStatus) -> None:
        self.translated_text = persian
        self.status = status

    def to_record(self, project_id: int | None = None) -> EntryRecord:
        return EntryRecord(
            project_id=project_id,
            position=self.position,
            sequence=self.sequence,
            start_ms=self.start_ms,
            end_ms=self.end_ms,
            original_text=self.original_text,
            working_text=self.working_text,
            translated_text=self.translated_text,
            status=self.status.value,
            speaker=self.speaker,
            source=self.source,
            correction_id=self.correction_id,
        )

    @classmethod
    def from_record(cls, record: EntryRecord) -> "SubtitleEntry":
        return cls(
            position=record.position,
            sequence=record.sequence,
            start_ms=record.start_ms,
            end_ms=record.end_ms,
            original_text=record.original_text,
            working_text=record.working_text,
            translated_text=record.translated_text,
            status=EntryStatus(record.status),
            speaker=record.speaker,
            source=record.source,
            correction_id=record.correction_id,
        )


@dataclass
class ParseIssue:
    block: int
    message: str
    severity: str = "warning"  # warning | error


@dataclass
class SubtitleDocument:
    entries: list[SubtitleEntry] = field(default_factory=list)
    path: str | None = None
    encoding: str = "utf-8"
    issues: list[ParseIssue] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.entries)

    def get(self, position: int) -> SubtitleEntry:
        return self.entries[position]

    def untranslated_positions(self) -> list[int]:
        return [
            e.position
            for e in self.entries
            if e.status in (EntryStatus.UNTRANSLATED, EntryStatus.FAILED)
        ]

    def translated_count(self) -> int:
        return sum(1 for e in self.entries if e.translated_text)

    def issue_summary(self) -> list[str]:
        return [f"block {i.block}: {i.message}" for i in self.issues]

    def to_records(self, project_id: int | None = None) -> list[EntryRecord]:
        return [e.to_record(project_id) for e in self.entries]

    @classmethod
    def from_records(cls, records: list[EntryRecord]) -> "SubtitleDocument":
        return cls(entries=[SubtitleEntry.from_record(r) for r in records])
