"""Sliding context windows around each batch.

The window (previous lines with their Persian translations, current lines,
upcoming originals, glossary hits, correction notes) gives the model continuity
across batch boundaries. ``line_context_hash`` provides the stable context key
used by the translation memory.

``ContextSource`` is the reserved seam for future context providers (OCR
history, game state, ...) — anything that can contribute extra prompt data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from app.core.subtitle_model import SubtitleDocument
from app.core.translation_memory import TranslationMemory


class ContextSource(Protocol):
    """Future extension point: an object with ``contribute(state) -> dict``."""

    def contribute(self, state: dict) -> dict: ...


@dataclass
class ContextLine:
    position: int
    text: str
    translation: str = ""
    speaker: str = ""


@dataclass
class TranslationContext:
    previous: list[ContextLine] = field(default_factory=list)
    lines: list[ContextLine] = field(default_factory=list)
    upcoming: list[str] = field(default_factory=list)
    glossary: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)  # populated by future ContextSources

    def to_prompt_dict(self) -> dict:
        return {
            "previous": [
                {"i": line.position, "en": line.text,
                 **({"fa": line.translation} if line.translation else {})}
                for line in self.previous
            ],
            "lines": [{"i": line.position, "en": line.text} for line in self.lines],
            "upcoming": self.upcoming,
            "glossary": self.glossary,
            "notes": self.notes,
        }


class ContextManager:
    def __init__(self, prev_lines: int = 3, future_lines: int = 2) -> None:
        self.prev_lines = max(0, prev_lines)
        self.future_lines = max(0, future_lines)
        self.extra_sources: list[ContextSource] = []  # future: OCR/game context

    def prev_texts(self, doc: SubtitleDocument, position: int) -> list[str]:
        start = max(0, position - self.prev_lines)
        return [doc.entries[p].working_text for p in range(start, position)]

    def build(
        self,
        doc: SubtitleDocument,
        positions: list[int],
        glossary_payload: list[dict] | None = None,
    ) -> TranslationContext:
        if not positions:
            return TranslationContext()
        first, last = positions[0], positions[-1]
        entries = doc.entries

        previous = [
            ContextLine(
                position=entries[p].position,
                text=entries[p].working_text,
                translation=entries[p].translated_text or "",
                speaker=entries[p].speaker,
            )
            for p in range(max(0, first - self.prev_lines), first)
        ]
        lines = [
            ContextLine(position=entries[p].position, text=entries[p].working_text,
                        speaker=entries[p].speaker)
            for p in positions
        ]
        upcoming = [entries[p].working_text for p in range(last + 1, min(len(entries), last + 1 + self.future_lines))]

        notes = []
        for p in positions:
            entry = entries[p]
            if entry.working_text != entry.original_text:
                notes.append(
                    f"Line {entry.position}: an approved automatic-caption correction is "
                    f"applied; original was {entry.original_text!r}"
                )

        context = TranslationContext(
            previous=previous, lines=lines, upcoming=upcoming,
            glossary=glossary_payload or [], notes=notes,
        )
        for source in self.extra_sources:  # currently unused; reserved seam
            context.extra.update(source.contribute(context.to_prompt_dict()))
        return context

    def line_context_hash(self, doc: SubtitleDocument, position: int) -> tuple[str, str]:
        """(source_hash, context_hash) for the translation memory."""
        source = doc.entries[position].working_text
        return (
            TranslationMemory.source_hash(source),
            TranslationMemory.context_hash_for(source, self.prev_texts(doc, position)),
        )
