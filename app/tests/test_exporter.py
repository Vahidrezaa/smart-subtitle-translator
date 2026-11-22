"""Tests for the SRT exporter: fidelity, encoding, untranslated fallback."""
from __future__ import annotations

from app.core.subtitle_model import SubtitleDocument, SubtitleEntry
from app.export.srt_exporter import export_srt, format_entry


def _doc() -> SubtitleDocument:
    return SubtitleDocument(
        entries=[
            SubtitleEntry(position=0, sequence=3, start_ms=1000, end_ms=3500,
                          original_text="Hello.", translated_text="سلام."),
            SubtitleEntry(position=1, sequence=7, start_ms=3500, end_ms=6000,
                          original_text="Second line", translated_text=None),
            SubtitleEntry(position=2, sequence=8, start_ms=6000, end_ms=8500,
                          original_text="Multi\nline", translated_text="چند\nخطی"),
        ],
        path="in.srt",
    )


class TestExport:
    def test_timestamps_sequences_and_order_preserved(self, tmp_path):
        result = export_srt(_doc(), tmp_path / "out.srt")
        content = result.path.read_bytes().decode("utf-8-sig")  # raw: no newline translation
        blocks = [b for b in content.split("\r\n\r\n") if b.strip()]
        assert len(blocks) == 3
        assert blocks[0].startswith("3\r\n00:00:01,000 --> 00:00:03,500\r\nسلام.")
        assert blocks[1].startswith("7\r\n00:00:03,500 --> 00:00:06,000\r\n")
        assert blocks[2].startswith("8\r\n00:00:06,000 --> 00:00:08,500\r\n")

    def test_untranslated_keeps_original_and_is_reported(self, tmp_path):
        result = export_srt(_doc(), tmp_path / "out.srt")
        content = result.path.read_text(encoding="utf-8")
        assert "Second line" in content
        assert result.untranslated_positions == [1]
        assert result.entries_written == 3

    def test_multiline_translation_inner_crlf(self, tmp_path):
        result = export_srt(_doc(), tmp_path / "out.srt")
        content = result.path.read_bytes().decode("utf-8-sig")
        assert "چند\r\nخطی" in content

    def test_bom_written_by_default(self, tmp_path):
        result = export_srt(_doc(), tmp_path / "out.srt")
        raw = result.path.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")

    def test_no_bom_when_disabled(self, tmp_path):
        result = export_srt(_doc(), tmp_path / "out.srt", bom=False)
        raw = result.path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")

    def test_lf_newline_option(self, tmp_path):
        result = export_srt(_doc(), tmp_path / "out.srt", use_crlf=False)
        content = result.path.read_text(encoding="utf-8")
        assert "-->" in content
        assert "\r" not in content

    def test_original_file_untouched(self, tmp_path):
        source = tmp_path / "in.srt"
        source.write_bytes(b"1\n00:00:01,000 --> 00:00:02,000\nHello.\n")
        doc = _doc()
        doc.path = str(source)
        export_srt(doc, tmp_path / "out.srt")
        assert source.read_bytes() == b"1\n00:00:01,000 --> 00:00:02,000\nHello.\n"

    def test_roundtrip_with_parser(self, tmp_path):
        from app.core.subtitle_parser import parse_srt_text

        result = export_srt(_doc(), tmp_path / "out.srt")
        content = result.path.read_text(encoding="utf-8-sig")
        reparsed = parse_srt_text(content)
        assert [e.sequence for e in reparsed.entries] == [3, 7, 8]
        assert [e.start_ms for e in reparsed.entries] == [1000, 3500, 6000]
        assert [e.end_ms for e in reparsed.entries] == [3500, 6000, 8500]
        assert reparsed.get(0).original_text == "سلام."
        assert reparsed.get(1).original_text == "Second line"


def test_format_entry_basic():
    text = format_entry(1, 0, 1500, "Hi there.", "\r\n")
    assert text == "1\r\n00:00:00,000 --> 00:00:01,500\r\nHi there.\r\n\r\n"
