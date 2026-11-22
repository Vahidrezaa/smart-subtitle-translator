"""Robust SRT parsing and loading.

Design goals: preserve sequence numbers and timestamps exactly, tolerate and
*report* malformed blocks instead of dying, support UTF-8 / UTF-8-BOM (with a
flagged cp1252 fallback), and keep the original text byte-for-byte where
possible. Implemented in-project (no external parser dependency) because the
validation/recovery behavior is a core requirement — see ARCHITECTURE.md §5.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from app.core.subtitle_model import EntryStatus, ParseIssue, SubtitleDocument, SubtitleEntry

log = logging.getLogger(__name__)

_TIME_RE = re.compile(
    r"^\s*(\d{1,2}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})\s*-->\s*"
    r"(\d{1,2}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})(.*)$"
)
_SEQ_RE = re.compile(r"^\s*(\d+)\s*$")


class SubtitleParseError(ValueError):
    """Raised when a file cannot be interpreted as SRT at all."""


def _components_to_ms(hours: str, minutes: str, seconds: str, fraction: str) -> int:
    millis = int(fraction.ljust(3, "0"))
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + millis


def parse_srt_text(
    text: str, path: str | None = None, encoding: str = "utf-8"
) -> SubtitleDocument:
    stripped = text.lstrip("\ufeff").lstrip()
    if stripped.upper().startswith("WEBVTT"):
        raise SubtitleParseError(
            "The file looks like WebVTT, not SRT. Re-export it as SRT and try again."
        )
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\ufeff", "")
    blocks = re.split(r"\n{2,}", normalized)
    document = SubtitleDocument(path=path, encoding=encoding)
    expected_sequence = 1
    last_end = -1

    for block_number, block in enumerate(blocks, start=1):
        lines = block.split("\n")
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        if not lines:
            continue

        sequence: int | None = None
        sequence_match = _SEQ_RE.match(lines[0])
        if sequence_match:
            sequence = int(sequence_match.group(1))
            lines = lines[1:]
            if not lines:
                document.issues.append(
                    ParseIssue(block_number, "Block has a sequence number but no content")
                )
                continue

        time_match = _TIME_RE.match(lines[0])
        if not time_match:
            document.issues.append(
                ParseIssue(
                    block_number,
                    f"Missing or malformed timing line: {lines[0][:60]!r}",
                    severity="error",
                )
            )
            continue

        start_ms = _components_to_ms(
            time_match.group(1), time_match.group(2), time_match.group(3), time_match.group(4)
        )
        end_ms = _components_to_ms(
            time_match.group(5), time_match.group(6), time_match.group(7), time_match.group(8)
        )
        cue_settings = (time_match.group(9) or "").strip()
        if cue_settings:
            document.issues.append(
                ParseIssue(block_number, f"Cue settings ignored: {cue_settings!r}")
            )

        text_content = "\n".join(lines[1:]).strip()

        if sequence is None:
            sequence = expected_sequence
            document.issues.append(
                ParseIssue(block_number, f"Missing sequence number; assigned {sequence}")
            )
        if sequence != expected_sequence:
            document.issues.append(
                ParseIssue(
                    block_number,
                    f"Sequence number {sequence} out of order (expected {expected_sequence})",
                )
            )
        if end_ms < start_ms:
            document.issues.append(
                ParseIssue(
                    block_number,
                    f"End time ({end_ms} ms) is before start time ({start_ms} ms)",
                    severity="error",
                )
            )
        if last_end >= 0 and start_ms < last_end:
            document.issues.append(
                ParseIssue(
                    block_number,
                    f"Overlaps the previous entry (starts at {start_ms} ms, "
                    f"previous ends at {last_end} ms)",
                )
            )

        entry = SubtitleEntry(
            position=len(document.entries),
            sequence=sequence,
            start_ms=start_ms,
            end_ms=end_ms,
            original_text=text_content,
        )
        if not text_content:
            document.issues.append(ParseIssue(block_number, "Empty caption text"))
            entry.status = EntryStatus.SKIPPED
        document.entries.append(entry)
        expected_sequence = sequence + 1
        last_end = max(last_end, end_ms)

    if not document.entries:
        raise SubtitleParseError("No subtitle entries found — is this an SRT file?")
    return document


def _decode(raw: bytes) -> tuple[str, str, str]:
    """Return (encoding, text, note). UTF-8 and UTF-8-BOM are required inputs;
    UTF-16 and cp1252 are tolerated with an explicit note."""
    if raw.startswith(b"\xef\xbb\xbf"):
        try:
            return "utf-8-sig", raw[3:].decode("utf-8"), ""
        except UnicodeDecodeError:
            pass
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16", raw.decode("utf-16"), ""
    try:
        return "utf-8", raw.decode("utf-8"), ""
    except UnicodeDecodeError as exc:
        return "cp1252", raw.decode("cp1252"), (
            "File is not valid UTF-8; decoded with cp1252 as a fallback "
            f"({exc}). Verify non-ASCII characters before translating."
        )


def load_srt_file(path: str | Path) -> SubtitleDocument:
    file_path = Path(path)
    raw = file_path.read_bytes()
    encoding, text, note = _decode(raw)
    document = parse_srt_text(text, path=str(file_path), encoding=encoding)
    if note:
        document.issues.insert(0, ParseIssue(0, note))
    log.info(
        "Loaded %d subtitle entries from %s (%s)", len(document.entries), file_path, encoding
    )
    return document
