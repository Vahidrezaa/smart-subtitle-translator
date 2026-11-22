"""Tests for SRT parsing, encoding handling and validation."""
from __future__ import annotations

import pytest

from app.core.subtitle_model import EntryStatus, ms_to_srt_time
from app.core.subtitle_parser import SubtitleParseError, load_srt_file, parse_srt_text

SAMPLE = """1
00:00:01,000 --> 00:00:03,500
Hello everyone.

2
00:00:03,500 --> 00:00:06,200
This is the second line,
split across two lines.

3
00:00:06,200 --> 00:00:09,000
And a third one.
"""


class TestBasicParsing:
    def test_parse_basic(self):
        doc = parse_srt_text(SAMPLE)
        assert len(doc) == 3
        first = doc.get(0)
        assert first.position == 0
        assert first.sequence == 1
        assert first.start_ms == 1000
        assert first.end_ms == 3500
        assert first.original_text == "Hello everyone."
        assert first.status is EntryStatus.UNTRANSLATED

    def test_multiline_text_preserved(self):
        doc = parse_srt_text(SAMPLE)
        assert doc.get(1).original_text == "This is the second line,\nsplit across two lines."

    def test_positions_and_sequences(self):
        doc = parse_srt_text(SAMPLE)
        assert [e.sequence for e in doc.entries] == [1, 2, 3]
        assert [e.position for e in doc.entries] == [0, 1, 2]

    def test_crlf_and_bom(self):
        doc = parse_srt_text("\ufeff" + SAMPLE.replace("\n", "\r\n"))
        assert len(doc) == 3
        assert doc.get(0).original_text == "Hello everyone."

    def test_dot_milliseconds_accepted(self):
        text = "1\n00:00:01.250 --> 00:00:03.750\nHi.\n"
        doc = parse_srt_text(text)
        assert doc.get(0).start_ms == 1250
        assert doc.get(0).end_ms == 3750


class TestMalformedInput:
    def test_missing_timing_line_is_reported_and_skipped(self):
        text = "1\n00:00:01,000 --> 00:00:02,000\nGood.\n\n2\nnot a timing line\nBad block.\n\n3\n00:00:03,000 --> 00:00:04,000\nFine.\n"
        doc = parse_srt_text(text)
        assert len(doc) == 2
        assert doc.get(0).original_text == "Good."
        assert doc.get(1).original_text == "Fine."
        errors = [i for i in doc.issues if i.severity == "error"]
        assert any("timing line" in i.message for i in errors)

    def test_missing_sequence_number_assigned(self):
        text = "00:00:01,000 --> 00:00:02,000\nNo sequence here.\n"
        doc = parse_srt_text(text)
        assert doc.get(0).sequence == 1
        assert any("Missing sequence number" in i.message for i in doc.issues)

    def test_out_of_order_sequence_warns(self):
        text = "1\n00:00:01,000 --> 00:00:02,000\nA.\n\n5\n00:00:02,000 --> 00:00:03,000\nB.\n"
        doc = parse_srt_text(text)
        assert any("out of order" in i.message for i in doc.issues)
        assert doc.get(1).sequence == 5

    def test_overlap_warns(self):
        text = (
            "1\n00:00:01,000 --> 00:00:04,000\nA.\n\n"
            "2\n00:00:03,000 --> 00:00:05,000\nB.\n"
        )
        doc = parse_srt_text(text)
        assert any("Overlaps" in i.message for i in doc.issues)

    def test_end_before_start_warns_as_error(self):
        text = "1\n00:00:05,000 --> 00:00:02,000\nA.\n"
        doc = parse_srt_text(text)
        issues = [i for i in doc.issues if i.severity == "error"]
        assert any("before start" in i.message for i in issues)

    def test_empty_text_kept_and_skipped(self):
        text = "1\n00:00:01,000 --> 00:00:02,000\n\n"
        doc = parse_srt_text(text)
        assert len(doc) == 1
        assert doc.get(0).status is EntryStatus.SKIPPED
        assert any("Empty caption" in i.message for i in doc.issues)

    def test_cue_settings_noted(self):
        text = "1\n00:00:01,000 --> 00:00:02,000 X1:40\nA.\n"
        doc = parse_srt_text(text)
        assert doc.get(0).original_text == "A."
        assert any("Cue settings" in i.message for i in doc.issues)

    def test_webvtt_rejected(self):
        with pytest.raises(SubtitleParseError):
            parse_srt_text("WEBVTT\n\n00:01.000 --> 00:02.000\nHi\n")

    def test_empty_file_rejected(self):
        with pytest.raises(SubtitleParseError):
            parse_srt_text("\n\n   \n")


class TestFileLoading:
    def test_load_utf8_bom(self, tmp_path):
        path = tmp_path / "sample.srt"
        path.write_bytes(b"\xef\xbb\xbf" + SAMPLE.encode("utf-8"))
        doc = load_srt_file(path)
        assert doc.encoding == "utf-8-sig"
        assert len(doc) == 3
        assert not any("UTF-8" in i.message and "cp1252" in i.message for i in doc.issues)

    def test_load_plain_utf8(self, tmp_path):
        path = tmp_path / "sample.srt"
        path.write_bytes(SAMPLE.encode("utf-8"))
        doc = load_srt_file(path)
        assert doc.encoding == "utf-8"

    def test_load_utf16(self, tmp_path):
        path = tmp_path / "sample.srt"
        path.write_bytes(SAMPLE.encode("utf-16"))
        doc = load_srt_file(path)
        assert doc.encoding == "utf-16"
        assert len(doc) == 3

    def test_cp1252_fallback_flagged(self, tmp_path):
        path = tmp_path / "legacy.srt"
        # é encoded as cp1252 byte 0xE9 — invalid UTF-8
        content = "1\n00:00:01,000 --> 00:00:02,000\ncaf\xe9 corner.\n"
        path.write_bytes(content.encode("cp1252"))
        doc = load_srt_file(path)
        assert doc.encoding == "cp1252"
        assert doc.get(0).original_text == "café corner."
        assert any("cp1252" in i.message for i in doc.issues)


class TestTimestampFormatting:
    @pytest.mark.parametrize(
        ("ms", "expected"),
        [
            (0, "00:00:00,000"),
            (1000, "00:00:01,000"),
            (3661500, "01:01:01,500"),
            (359999999, "99:59:59,999"),
            (-5, "00:00:00,000"),
        ],
    )
    def test_ms_to_srt_time(self, ms, expected):
        assert ms_to_srt_time(ms) == expected
