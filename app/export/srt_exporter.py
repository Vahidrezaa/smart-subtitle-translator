"""SRT export: writes the Persian SRT, preserving timestamps and order.

The input file is never touched — export always writes a new file. Lines that
were never translated keep their original text and are reported in the result,
so nothing is silently dropped.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from app.core.subtitle_model import SubtitleDocument, ms_to_srt_time

log = logging.getLogger(__name__)


@dataclass
class ExportResult:
    path: Path
    entries_written: int
    untranslated_positions: list[int] = field(default_factory=list)


def format_entry(sequence: int, start_ms: int, end_ms: int, text: str, newline: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)
    return (
        f"{sequence}{newline}"
        f"{ms_to_srt_time(start_ms)} --> {ms_to_srt_time(end_ms)}{newline}"
        f"{text}{newline}{newline}"
    )


def export_srt(
    doc: SubtitleDocument,
    out_path: str | Path,
    bom: bool = True,
    use_crlf: bool = True,
) -> ExportResult:
    """Write the translated SRT.

    ``bom=True`` writes UTF-8 with BOM (helps players like PotPlayer detect
    the encoding reliably); ``use_crlf`` uses Windows line endings, the most
    compatible choice for desktop players.
    """
    newline = "\r\n" if use_crlf else "\n"
    encoding = "utf-8-sig" if bom else "utf-8"
    untranslated: list[int] = []
    parts: list[str] = []
    for entry in doc.entries:
        text = entry.translated_text if entry.translated_text and entry.translated_text.strip() else None
        if text is None:
            untranslated.append(entry.position)
            text = entry.original_text
        parts.append(format_entry(entry.sequence, entry.start_ms, entry.end_ms, text, newline))

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(parts), encoding=encoding, newline="")
    if untranslated:
        log.warning("Exported with %d untranslated lines (original text kept): %s",
                    len(untranslated), untranslated)
    return ExportResult(path=out, entries_written=len(doc.entries),
                        untranslated_positions=untranslated)
