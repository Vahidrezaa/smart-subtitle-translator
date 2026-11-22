"""Corrections review dialog: scan, inspect in context, approve or reject.

Nothing is ever applied automatically — approving a proposal updates
``working_text`` (the translation source) while ``original_text`` stays intact.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.core.correction_engine import CorrectionEngine
from app.core.glossary_manager import GlossaryManager
from app.core.subtitle_model import SubtitleDocument
from app.database.database_manager import DatabaseManager
from app.gui.strings import FA
from app.gui.workers import JobWorker
from app.providers.base_provider import TranslationProvider

_REVIEW_COLUMNS = (
    FA["col_position"], FA["col_original_text"], FA["col_proposed"],
    FA["col_confidence"], FA["col_reason"], FA["col_method"],
)


class ReviewDialog(QDialog):
    def __init__(
        self,
        parent,
        db: DatabaseManager,
        glossary: GlossaryManager,
        get_doc,
        get_project_id,
        make_provider,
        on_doc_changed,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._glossary = glossary
        self._get_doc = get_doc
        self._get_project_id = get_project_id
        self._make_provider = make_provider
        self._on_doc_changed = on_doc_changed
        self._engine: CorrectionEngine | None = None
        self._worker: JobWorker | None = None
        self.setWindowTitle(FA["dlg_review_title"])
        self.setMinimumSize(880, 600)
        self._build_ui()
        self._reload()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        buttons_row = QHBoxLayout()
        scan_button = QPushButton(FA["dlg_review_scan"])
        scan_button.clicked.connect(self._on_scan)
        buttons_row.addWidget(scan_button)
        llm_button = QPushButton(FA["dlg_review_llm"])
        llm_button.clicked.connect(self._on_llm_review)
        buttons_row.addWidget(llm_button)
        buttons_row.addStretch(1)
        layout.addLayout(buttons_row)

        self.table = QTableWidget(0, len(_REVIEW_COLUMNS))
        self.table.setHorizontalHeaderLabels(list(_REVIEW_COLUMNS))
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self.table)

        self.context_label = QLabel(FA["dlg_review_context"])
        self.context_label.setWordWrap(True)
        layout.addWidget(self.context_label)

        actions_row = QHBoxLayout()
        approve_button = QPushButton(FA["dlg_review_approve"])
        approve_button.clicked.connect(self._on_approve)
        actions_row.addWidget(approve_button)
        reject_button = QPushButton(FA["dlg_review_reject"])
        reject_button.clicked.connect(self._on_reject)
        actions_row.addWidget(reject_button)
        actions_row.addStretch(1)
        layout.addLayout(actions_row)

        close_box = QHBoxLayout()
        close_box.addStretch(1)
        close_button = QPushButton(FA["dlg_review_close"])
        close_button.clicked.connect(self.accept)
        close_box.addWidget(close_button)
        layout.addLayout(close_box)

    # -------------------------------------------------------------- helpers
    def _doc(self) -> SubtitleDocument | None:
        return self._get_doc()

    def _engine_for_doc(self, doc: SubtitleDocument) -> CorrectionEngine:
        provider: TranslationProvider | None = None
        try:
            provider = self._make_provider()
        except Exception:
            provider = None  # heuristics work without a provider
        return CorrectionEngine(self._db, self._glossary, provider=provider)

    def _reload(self) -> None:
        doc = self._doc()
        project_id = self._get_project_id()
        if doc is None or project_id is None:
            self.table.setRowCount(0)
            return
        records = self._db.correction_list(project_id, status="proposed")
        self.table.setRowCount(len(records))
        for row, record in enumerate(records):
            values = [
                str(record.position),
                record.original_text,
                record.proposed_text,
                f"{record.confidence:.2f}",
                record.reason,
                record.method,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, record.id)
                self.table.setItem(row, column, item)
        if not records:
            self.context_label.setText(FA["dlg_review_none"])
        else:
            self.context_label.setText(FA["dlg_review_context"])

    def _selected_id(self) -> int | None:
        items = self.table.selectedItems()
        if not items:
            return None
        return items[0].data(Qt.UserRole)

    def _on_selection_changed(self) -> None:
        doc = self._doc()
        if doc is None:
            return
        correction_id = self._selected_id()
        if correction_id is None:
            return
        record = self._db.correction_get(correction_id)
        lines = []
        for position in range(max(0, record.position - 2), record.position + 3):
            if position >= len(doc):
                break
            marker = " >>" if position == record.position else "   "
            lines.append(f"{marker} [{position}] {doc.get(position).original_text}")
        self.context_label.setText(FA["dlg_review_context"] + "\n" + "\n".join(lines))

    # -------------------------------------------------------------- actions
    def _on_scan(self) -> None:
        doc = self._doc()
        project_id = self._get_project_id()
        if doc is None or project_id is None:
            QMessageBox.information(self, FA["dlg_review_title"], FA["dlg_review_need_doc"])
            return
        engine = self._engine_for_doc(doc)
        proposals = engine.scan_heuristics(doc, project_id)
        self._reload()
        if proposals:
            QMessageBox.information(
                self, FA["dlg_review_title"],
                FA["dlg_review_done"].format(n=len(proposals)),
            )

    def _on_llm_review(self) -> None:
        doc = self._doc()
        project_id = self._get_project_id()
        if doc is None or project_id is None:
            QMessageBox.information(self, FA["dlg_review_title"], FA["dlg_review_need_doc"])
            return
        try:
            provider = self._make_provider()
        except Exception as exc:
            QMessageBox.warning(self, FA["dlg_review_llm"], str(exc))
            return
        engine = CorrectionEngine(self._db, self._glossary, provider=provider)

        def job() -> int:
            proposals = engine.review_with_llm(doc, project_id)
            return len(proposals)

        self._worker = JobWorker(job, self)
        self._worker.done.connect(self._on_llm_done)
        self._worker.failed.connect(
            lambda message: QMessageBox.warning(self, FA["dlg_review_llm"], message)
        )
        self._worker.start()

    def _on_llm_done(self, count: int) -> None:
        self._reload()
        QMessageBox.information(self, FA["dlg_review_llm"], FA["dlg_review_done"].format(n=count))

    def _on_approve(self) -> None:
        doc = self._doc()
        project_id = self._get_project_id()
        correction_id = self._selected_id()
        if doc is None or project_id is None or correction_id is None:
            QMessageBox.information(self, FA["dlg_review_title"], FA["dlg_review_select"])
            return
        engine = self._engine_for_doc(doc)
        engine.approve(doc, correction_id)
        entry = doc.get(self._db.correction_get(correction_id).position)
        self._db.entry_set_working_text(project_id, entry.position, entry.working_text,
                                        entry.correction_id)
        self._reload()
        self._on_doc_changed()
        QMessageBox.information(self, FA["dlg_review_title"], FA["dlg_review_applied"])

    def _on_reject(self) -> None:
        doc = self._doc()
        project_id = self._get_project_id()
        correction_id = self._selected_id()
        if doc is None or project_id is None or correction_id is None:
            QMessageBox.information(self, FA["dlg_review_title"], FA["dlg_review_select"])
            return
        engine = self._engine_for_doc(doc)
        record = engine.reject(doc, correction_id)
        entry = doc.get(record.position)
        self._db.entry_set_working_text(project_id, entry.position, entry.working_text,
                                        entry.correction_id)
        self._reload()
        self._on_doc_changed()
        QMessageBox.information(self, FA["dlg_review_title"], FA["dlg_review_rejected"])
