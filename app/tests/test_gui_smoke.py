"""GUI smoke tests — run offscreen (QT_QPA_PLATFORM=offscreen).

These verify that the window, model, and dialogs construct and behave
logically. They do not verify visual layout on a real desktop.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app.config.app_config import Settings
from app.core.subtitle_model import EntryStatus, SubtitleDocument, SubtitleEntry
from app.database.database_manager import DatabaseManager
from app.gui.table_model import SubtitlesTableModel

SAMPLE_DOC = SubtitleDocument(
    entries=[
        SubtitleEntry(position=i, sequence=i + 1, start_ms=i * 2000, end_ms=i * 2000 + 1500,
                      original_text=text)
        for i, text in enumerate(["Hello world.", "Second line here.", "Third line."])
    ]
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def window(qapp, tmp_path):
    from app.gui.main_window import MainWindow

    db = DatabaseManager(tmp_path / "gui.db")
    db.migrate()
    win = MainWindow(Settings(), db, tmp_path)
    yield win
    db.close()


class TestTableModel:
    def test_row_and_column_counts(self, qapp):
        model = SubtitlesTableModel(SAMPLE_DOC)
        assert model.rowCount() == 3
        assert model.columnCount() == 5

    def test_display_values(self, qapp):
        model = SubtitlesTableModel(SAMPLE_DOC)
        assert model.data(model.index(0, model.SEQ)) == 1
        assert model.data(model.index(0, model.START)) == "00:00:00,000"
        assert model.data(model.index(1, model.END)) == "00:00:03,500"
        assert model.data(model.index(2, model.ORIGINAL)) == "Third line."
        assert model.data(model.index(0, model.PERSIAN)) == ""

    def test_edit_persian_marks_corrected(self, qapp):
        model = SubtitlesTableModel(SAMPLE_DOC)
        index = model.index(0, model.PERSIAN)
        assert model.setData(index, "سلام دنیا.")
        entry = SAMPLE_DOC.get(0)
        assert entry.translated_text == "سلام دنیا."
        assert entry.status is EntryStatus.CORRECTED
        assert model.data(index) == "سلام دنیا."

    def test_empty_edit_rejected(self, qapp):
        model = SubtitlesTableModel(SAMPLE_DOC)
        assert not model.setData(model.index(0, model.PERSIAN), "   ")

    def test_status_background_colors(self, qapp):
        SAMPLE_DOC.get(1).status = EntryStatus.FAILED
        model = SubtitlesTableModel(SAMPLE_DOC)
        assert model.data(model.index(1, model.PERSIAN), Qt.BackgroundRole) is not None


class TestMainWindow:
    def test_constructs_and_starts_empty(self, window):
        assert window.model.rowCount() == 0
        assert not window.act_start.isEnabled()
        assert not window.act_export.isEnabled()
        assert window.language_combo.currentData() == "fa"

    def test_load_document_populates_table(self, window):
        window._load_document(SAMPLE_DOC)
        assert window.model.rowCount() == 3
        assert window.act_start.isEnabled()
        assert window.act_export.isEnabled()
        assert window.progress.maximum() == 3

    def test_layout_direction_is_rtl(self, window):
        assert window.layoutDirection() == Qt.RightToLeft

    def test_manual_edit_updates_document(self, window):
        window._load_document(SAMPLE_DOC)
        index = window.model.index(0, SubtitlesTableModel.PERSIAN)
        assert window.model.setData(index, "ترجمه دستی")
        assert window._doc.get(0).status is EntryStatus.CORRECTED

    def test_dialogs_construct(self, window, tmp_path):
        from app.gui.api_settings_dialog import ApiSettingsDialog
        from app.gui.glossary_dialog import GlossaryDialog
        from app.gui.review_dialog import ReviewDialog

        api_dialog = ApiSettingsDialog(window, window.settings)
        assert api_dialog.provider_combo.count() >= 2
        assert api_dialog.model_edit.text() == "gpt-4o-mini"
        api_dialog.deleteLater()

        glossary_dialog = GlossaryDialog(window, window.glossary)
        assert glossary_dialog.table.rowCount() == 0
        glossary_dialog.deleteLater()

        review_dialog = ReviewDialog(
            window, window.db, window.glossary,
            get_doc=lambda: window._doc,
            get_project_id=lambda: window._project_id,
            make_provider=lambda: (_ for _ in ()).throw(RuntimeError("no provider in test")),
            on_doc_changed=lambda: None,
        )
        assert review_dialog.table.rowCount() == 0
        review_dialog.deleteLater()

    def test_glossary_dialog_lists_entries(self, window):
        from app.database.models import GlossaryEntry
        from app.gui.glossary_dialog import GlossaryDialog

        window.glossary.add(GlossaryEntry(english_term="cache",
                                          persian_primary="حافظه نهان"))
        dialog = GlossaryDialog(window, window.glossary)
        assert dialog.table.rowCount() == 1
        dialog.deleteLater()


def test_main_module_importable():
    import app.main  # noqa: F401  (entry point wiring)
