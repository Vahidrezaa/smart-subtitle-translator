"""Main window: toolbar, subtitle table, progress, and all actions wired."""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QStatusBar,
    QTableView,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from app.config.app_config import Settings
from app.config.credentials import CredentialError, resolve_api_key
from app.core.batch_processor import BatchProcessor
from app.core.context_manager import ContextManager
from app.core.events import ProgressEvent
from app.core.glossary_manager import GlossaryManager
from app.core.languages import LANGUAGES
from app.core.subtitle_model import EntryStatus, SubtitleDocument
from app.core.subtitle_parser import SubtitleParseError, load_srt_file
from app.core.textutils import stable_hash
from app.core.translation_engine import TranslationEngine
from app.core.translation_memory import MemoryPolicy, TranslationMemory
from app.database.database_manager import DatabaseManager
from app.database.models import ProjectRecord
from app.export.srt_exporter import export_srt
from app.gui.api_settings_dialog import ApiSettingsDialog
from app.gui.glossary_dialog import GlossaryDialog
from app.gui.review_dialog import ReviewDialog
from app.gui.strings import FA
from app.gui.table_model import SubtitlesTableModel
from app.gui.workers import TranslationWorker
from app.providers import create_provider

log = logging.getLogger(__name__)

STYLESHEET = """
QMainWindow { background: #f7f8fa; }
QToolBar { background: #ffffff; border-bottom: 1px solid #dcdfe4; padding: 6px; spacing: 4px; }
QToolButton { padding: 6px 12px; border-radius: 6px; }
QToolButton:hover { background: #eef1f6; }
QTableView { background: #ffffff; gridline-color: #eceff3; alternate-background-color: #fafbfd; }
QHeaderView::section { background: #f0f2f6; padding: 6px; border: none; font-weight: bold; }
QProgressBar { border: 1px solid #dcdfe4; border-radius: 6px; text-align: center; max-height: 16px; }
QProgressBar::chunk { background: #4f8cff; border-radius: 5px; }
"""


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings, db: DatabaseManager, data_dir: Path) -> None:
        super().__init__()
        self.settings = settings
        self.db = db
        self.data_dir = data_dir
        self.glossary = GlossaryManager(db)
        self.memory = self._make_memory()
        self.context_manager = self._make_context()
        self._doc: SubtitleDocument | None = None
        self._project_id: int | None = None
        self._worker: TranslationWorker | None = None
        self._build_ui()

    # ------------------------------------------------------------------ setup
    def _make_memory(self) -> TranslationMemory:
        return TranslationMemory(self.db, MemoryPolicy(**vars(self.settings.memory)))

    def _make_context(self) -> ContextManager:
        return ContextManager(
            self.settings.translation.context_prev_lines,
            self.settings.translation.context_future_lines,
        )

    def _build_ui(self) -> None:
        self.setWindowTitle(FA["app_title"])
        self.setMinimumSize(1000, 640)
        self.setStyleSheet(STYLESHEET)
        self.setLayoutDirection(Qt.RightToLeft)

        toolbar = QToolBar("main")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self.act_import = QAction(FA["import_srt"], self)
        self.act_import.triggered.connect(self._on_import)
        toolbar.addAction(self.act_import)

        self.act_api = QAction(FA["api_settings"], self)
        self.act_api.triggered.connect(self._on_api_settings)
        toolbar.addAction(self.act_api)

        self.act_glossary = QAction(FA["glossary"], self)
        self.act_glossary.triggered.connect(self._on_glossary)
        toolbar.addAction(self.act_glossary)

        self.act_review = QAction(FA["review"], self)
        self.act_review.triggered.connect(self._on_review)
        toolbar.addAction(self.act_review)

        toolbar.addSeparator()

        self.act_start = QAction(FA["start_translation"], self)
        self.act_start.triggered.connect(self._on_start_translation)
        toolbar.addAction(self.act_start)

        self.act_cancel = QAction(FA["cancel"], self)
        self.act_cancel.triggered.connect(self._on_cancel)
        self.act_cancel.setEnabled(False)
        toolbar.addAction(self.act_cancel)

        self.act_retry = QAction(FA["retry_failed"], self)
        self.act_retry.triggered.connect(self._on_retry_failed)
        toolbar.addAction(self.act_retry)

        toolbar.addSeparator()

        self.act_export = QAction(FA["export_srt"], self)
        self.act_export.triggered.connect(self._on_export)
        toolbar.addAction(self.act_export)

        central = QWidget()
        layout = QVBoxLayout(central)

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel(FA["target_language"]))
        self.language_combo = QComboBox()
        for info in LANGUAGES.values():
            self.language_combo.addItem(f"{info.native_name} ({info.english_name})", info.code)
        self.language_combo.setCurrentIndex(0)
        top_row.addWidget(self.language_combo)
        top_row.addStretch(1)
        layout.addLayout(top_row)

        self.table = QTableView()
        self.model = SubtitlesTableModel(headers=(
            FA["col_seq"], FA["col_start"], FA["col_end"],
            FA["col_original"], FA["col_persian"],
        ))
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        for column in (SubtitlesTableModel.ORIGINAL, SubtitlesTableModel.PERSIAN):
            header.setSectionResizeMode(column, header.ResizeMode.Stretch)
        self.table.setColumnWidth(SubtitlesTableModel.SEQ, 60)
        self.table.setColumnWidth(SubtitlesTableModel.START, 110)
        self.table.setColumnWidth(SubtitlesTableModel.END, 110)
        layout.addWidget(self.table)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        self.setCentralWidget(central)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage(FA["status_ready"])
        self._update_action_states()

    def _update_action_states(self) -> None:
        has_doc = self._doc is not None
        running = self._worker is not None
        self.act_import.setEnabled(not running)
        self.act_start.setEnabled(has_doc and not running)
        self.act_cancel.setEnabled(running)
        self.act_retry.setEnabled(has_doc and not running)
        self.act_export.setEnabled(has_doc and not running)
        self.act_glossary.setEnabled(not running)
        self.act_review.setEnabled(has_doc and not running)
        self.act_api.setEnabled(not running)

    # ----------------------------------------------------------------- import
    def _on_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, FA["import_srt"], "", "SubRip subtitles (*.srt);;All files (*.*)"
        )
        if not path:
            return
        try:
            doc = load_srt_file(path)
        except (SubtitleParseError, OSError) as exc:
            QMessageBox.critical(self, FA["import_srt"], f"خطا در بارگذاری فایل:\n{exc}")
            return
        file_hash = self._file_hash(path)
        project = self.db.project_find_by_file(path, file_hash)
        note = ""
        if project is not None:
            self._overlay_project_state(doc, project.id)
            self._project_id = project.id
            note = FA["msg_resumed"]
        else:
            self._project_id = self.db.project_create(ProjectRecord(
                name=Path(path).stem, srt_path=path, file_hash=file_hash,
                target_language=self.settings.translation.target_language,
            ))
            self.db.entries_replace(self._project_id, doc.to_records(self._project_id))
        doc.path = path
        self._load_document(doc)
        issues = doc.issue_summary()
        if issues:
            preview = "\n".join(issues[:8])
            if len(issues) > 8:
                preview += f"\n... و {len(issues) - 8} مورد دیگر"
            QMessageBox.warning(self, FA["import_srt"], FA["msg_parse_issues"] + "\n" + preview)
        if note:
            self.status_bar.showMessage(f"{note} — {len(doc)} خط", 15000)

    @staticmethod
    def _file_hash(path: str) -> str:
        return stable_hash(Path(path).read_bytes().decode("utf-8", errors="replace"))

    def _overlay_project_state(self, doc: SubtitleDocument, project_id: int) -> None:
        records = {r.position: r for r in self.db.entries_load(project_id)}
        for entry in doc.entries:
            record = records.get(entry.position)
            if record is None:
                continue
            entry.working_text = record.working_text
            entry.translated_text = record.translated_text
            entry.status = EntryStatus(record.status)
            entry.correction_id = record.correction_id

    def _load_document(self, doc: SubtitleDocument) -> None:
        self._doc = doc
        self.model.set_document(doc)
        self.progress.setMaximum(max(1, len(doc)))
        self.progress.setValue(doc.translated_count())
        self._update_action_states()
        self.status_bar.showMessage(FA["msg_loaded"].format(n=len(doc)) + f" — {doc.path}")

    # ------------------------------------------------------------- translation
    def _make_provider(self):
        api = self.settings.api
        api_key = resolve_api_key(api.credential_backend)
        return create_provider(
            api.provider,
            base_url=api.base_url,
            api_key=api_key,
            model=api.model,
            temperature=api.temperature,
            timeout=api.timeout_seconds,
        )

    def _on_start_translation(self) -> None:
        if self._worker is not None or self._doc is None or self._project_id is None:
            return
        if not self._doc.untranslated_positions():
            QMessageBox.information(self, FA["start_translation"], FA["msg_nothing_to_translate"])
            return
        try:
            provider = self._make_provider()
        except CredentialError:
            QMessageBox.warning(self, FA["api_settings"], FA["msg_credential_missing"])
            self._on_api_settings()
            return
        problems = provider.validate_config()
        if problems:
            QMessageBox.warning(
                self, FA["api_settings"],
                FA["msg_provider_problems"].format(problems="\n".join(problems)),
            )
            self._on_api_settings()
            return

        engine = TranslationEngine(
            provider, self.glossary, self.context_manager,
            target_language=self.settings.translation.target_language,
        )
        doc = self._doc
        project_id = self._project_id
        settings = self.settings

        def build_processor(cancel_check, listener):
            return BatchProcessor(
                db=self.db, engine=engine, memory=self.memory,
                context_manager=self.context_manager, glossary=self.glossary,
                max_lines=settings.translation.batch_max_lines,
                max_chars=settings.translation.batch_max_chars,
                max_retries=settings.translation.max_retries,
                retry_base_delay=settings.translation.retry_base_delay,
                model=settings.api.model,
                source_file=str(doc.path or ""),
                listener=listener,
                cancel_check=cancel_check,
            )

        self._worker = TranslationWorker(build_processor, project_id, doc)
        self._worker.progress.connect(self._on_progress)
        self._worker.line_done.connect(self._on_line_done)
        self._worker.retrying.connect(
            lambda message: self.status_bar.showMessage(message, 12000))
        self._worker.warning.connect(
            lambda message: self.status_bar.showMessage(message, 15000))
        self._worker.run_finished.connect(self._on_run_finished)
        self.progress.setMaximum(max(1, len(doc.untranslated_positions())))
        self.progress.setValue(0)
        self._update_action_states()
        self.status_bar.showMessage(FA["msg_started"])
        self._worker.start()

    def _on_progress(self, done: int, total: int, message: str) -> None:
        self.progress.setMaximum(max(1, total))
        self.progress.setValue(done)
        if message:
            self.status_bar.showMessage(message, 20000)

    def _on_line_done(self, position: int, reused: bool) -> None:
        self.model.refresh_entry(position)
        self.progress.setValue(self.progress.value() + 1)

    def _on_run_finished(self, ok: bool, message: str) -> None:
        if self._worker is not None:
            self._worker.wait()
        self._worker = None
        if self._doc is not None:
            self.progress.setMaximum(max(1, len(self._doc)))
            self.progress.setValue(self._doc.translated_count())
        self.model.refresh_all()
        self._update_action_states()
        self.status_bar.showMessage(message, 30000)
        if not ok:
            QMessageBox.warning(self, FA["start_translation"], message)

    def _on_cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self.status_bar.showMessage("در حال توقف پس از پایان دسته جاری...")

    def _on_retry_failed(self) -> None:
        if self._doc is None or self._project_id is None:
            return
        failed = [e.position for e in self._doc.entries if e.status is EntryStatus.FAILED]
        if not failed:
            QMessageBox.information(self, FA["retry_failed"], FA["msg_no_failed"])
            return
        for position in failed:
            entry = self._doc.get(position)
            entry.status = EntryStatus.UNTRANSLATED
            self.db.entry_set_translation(self._project_id, position, None,
                                          EntryStatus.UNTRANSLATED.value)
        self.model.refresh_all()
        self._on_start_translation()

    # ---------------------------------------------------------------- dialogs
    def _on_api_settings(self) -> None:
        dialog = ApiSettingsDialog(self, self.settings)
        if dialog.exec():
            # pick up batching / memory changes without a restart
            self.memory = self._make_memory()
            self.context_manager = self._make_context()
            self.status_bar.showMessage("تنظیمات ذخیره شد.", 10000)

    def _on_glossary(self) -> None:
        GlossaryDialog(self, self.glossary).exec()

    def _on_review(self) -> None:
        if self._doc is None or self._project_id is None:
            QMessageBox.information(self, FA["review"], FA["dlg_review_need_doc"])
            return
        ReviewDialog(
            self, self.db, self.glossary,
            get_doc=lambda: self._doc,
            get_project_id=lambda: self._project_id,
            make_provider=self._make_provider,
            on_doc_changed=self.model.refresh_all,
        ).exec()

    # ----------------------------------------------------------------- export
    def _on_export(self) -> None:
        if self._doc is None:
            QMessageBox.information(self, FA["export_srt"], FA["msg_need_import"])
            return
        stem = Path(self._doc.path or "subtitles").stem
        path, _ = QFileDialog.getSaveFileName(
            self, FA["export_srt"], f"{stem}.fa.srt", "SubRip subtitles (*.srt)"
        )
        if not path:
            return
        result = export_srt(
            self._doc, path,
            bom=self.settings.export.bom,
            use_crlf=self.settings.export.use_crlf,
        )
        if result.untranslated_positions:
            self.status_bar.showMessage(
                f"خطوط بدون ترجمه: {result.untranslated_positions}", 20000)
            QMessageBox.warning(
                self, FA["export_srt"],
                FA["msg_export_partial"].format(n=len(result.untranslated_positions)),
            )
        else:
            QMessageBox.information(
                self, FA["export_srt"], FA["msg_export_ok"].format(path=result.path)
            )

    # ---------------------------------------------------------------- cleanup
    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        if self._worker is not None:
            self._worker.cancel()
            self._worker.wait(5000)
        super().closeEvent(event)
