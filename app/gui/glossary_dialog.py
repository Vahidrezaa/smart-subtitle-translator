"""Glossary manager dialog: add / update / delete terminology entries."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.core.glossary_manager import GlossaryManager
from app.database.database_manager import DuplicateTermError
from app.database.models import GlossaryEntry
from app.gui.strings import FA

_COLUMNS = (
    FA["col_term"], FA["col_equivalent"], FA["col_alternatives"],
    FA["col_category"], "توضیح",
)


class GlossaryDialog(QDialog):
    def __init__(self, parent, glossary: GlossaryManager) -> None:
        super().__init__(parent)
        self._glossary = glossary
        self.setWindowTitle(FA["dlg_glossary_title"])
        self.setMinimumSize(760, 560)
        self._build_ui()
        self._reload()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel(FA["dlg_glossary_search"]))
        self.search_edit = QLineEdit()
        self.search_edit.textChanged.connect(self._reload)
        search_row.addWidget(self.search_edit)
        layout.addLayout(search_row)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(list(_COLUMNS))
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self.table)

        form = QFormLayout()
        self.term_edit = QLineEdit()
        form.addRow(FA["dlg_glossary_term"], self.term_edit)
        self.primary_edit = QLineEdit()
        form.addRow(FA["dlg_glossary_primary"], self.primary_edit)
        self.alternatives_edit = QLineEdit()
        form.addRow(FA["dlg_glossary_alt"], self.alternatives_edit)
        self.category_edit = QLineEdit()
        form.addRow(FA["dlg_glossary_category"], self.category_edit)
        self.explanation_edit = QPlainTextEdit()
        self.explanation_edit.setMaximumHeight(70)
        form.addRow(FA["dlg_glossary_explanation"], self.explanation_edit)
        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setMaximumHeight(60)
        form.addRow(FA["dlg_glossary_notes"], self.notes_edit)
        layout.addLayout(form)

        buttons_row = QHBoxLayout()
        add_button = QPushButton(FA["dlg_glossary_add"])
        add_button.clicked.connect(self._on_add)
        buttons_row.addWidget(add_button)
        update_button = QPushButton(FA["dlg_glossary_update"])
        update_button.clicked.connect(self._on_update)
        buttons_row.addWidget(update_button)
        delete_button = QPushButton(FA["dlg_glossary_delete"])
        delete_button.clicked.connect(self._on_delete)
        buttons_row.addWidget(delete_button)
        buttons_row.addStretch(1)
        layout.addLayout(buttons_row)

        close_box = QDialogButtonBox(QDialogButtonBox.Close)
        close_box.rejected.connect(self.reject)
        layout.addWidget(close_box)

    # ------------------------------------------------------------- data ops
    def _reload(self) -> None:
        entries = self._glossary.list_entries(search=self.search_edit.text().strip())
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            values = [
                entry.english_term,
                entry.persian_primary,
                "، ".join(entry.persian_alternatives),
                entry.category,
                entry.explanation,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, entry.id)
                self.table.setItem(row, column, item)

    def _selected_id(self) -> int | None:
        items = self.table.selectedItems()
        if not items:
            return None
        return items[0].data(Qt.UserRole)

    def _form_entry(self) -> tuple[GlossaryEntry, None] | tuple[None, str]:
        term = self.term_edit.text().strip()
        primary = self.primary_edit.text().strip()
        if not term or not primary:
            return None, FA["dlg_glossary_empty_form"]
        alternatives = [
            part.strip() for part in self.alternatives_edit.text().split("،") if part.strip()
        ] + [part.strip() for part in self.alternatives_edit.text().split(",") if part.strip()]
        # de-duplicate while preserving order
        seen, alternatives = set(), [a for a in alternatives if not (a in seen or seen.add(a))]
        entry = GlossaryEntry(
            english_term=term,
            persian_primary=primary,
            persian_alternatives=alternatives,
            category=self.category_edit.text().strip(),
            explanation=self.explanation_edit.toPlainText().strip(),
            notes=self.notes_edit.toPlainText().strip(),
        )
        return entry, None

    def _on_add(self) -> None:
        entry, error = self._form_entry()
        if error:
            QMessageBox.warning(self, FA["dlg_glossary_title"], error)
            return
        try:
            self._glossary.add(entry)
        except DuplicateTermError as exc:
            QMessageBox.warning(self, FA["dlg_glossary_title"], str(exc))
            return
        self._reload()

    def _on_selection_changed(self) -> None:
        entry_id = self._selected_id()
        if entry_id is None:
            return
        entry = self._glossary.get(entry_id)
        if entry is None:
            return
        self.term_edit.setText(entry.english_term)
        self.primary_edit.setText(entry.persian_primary)
        self.alternatives_edit.setText(", ".join(entry.persian_alternatives))
        self.category_edit.setText(entry.category)
        self.explanation_edit.setPlainText(entry.explanation)
        self.notes_edit.setPlainText(entry.notes)

    def _on_update(self) -> None:
        entry_id = self._selected_id()
        if entry_id is None:
            QMessageBox.information(self, FA["dlg_glossary_title"], FA["dlg_glossary_select_first"])
            return
        entry, error = self._form_entry()
        if error:
            QMessageBox.warning(self, FA["dlg_glossary_title"], error)
            return
        self._glossary.update(
            entry_id,
            english_term=entry.english_term,
            persian_primary=entry.persian_primary,
            persian_alternatives=entry.persian_alternatives,
            category=entry.category,
            explanation=entry.explanation,
            notes=entry.notes,
        )
        self._reload()

    def _on_delete(self) -> None:
        entry_id = self._selected_id()
        if entry_id is None:
            QMessageBox.information(self, FA["dlg_glossary_title"], FA["dlg_glossary_select_first"])
            return
        answer = QMessageBox.question(
            self, FA["dlg_glossary_delete"], FA["dlg_glossary_confirm_delete"],
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self._glossary.delete(entry_id)
        self._reload()
