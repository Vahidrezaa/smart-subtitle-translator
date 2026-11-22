"""API + translation settings dialog (provider, model, key, batching, memory)."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from app.config.app_config import Settings
from app.config.credentials import CredentialError, get_store, resolve_api_key
from app.config.settings_store import save_settings
from app.gui.strings import FA
from app.gui.workers import ConnectionTestWorker
from app.providers import available_providers, create_provider

_BACKENDS = (("auto", "خودکار (توصیه‌شده)"), ("keyring", "مدیریت اعتبارنامه ویندوز"),
             ("env", "متغیر محیطی SST_API_KEY"), ("file", "فایل محلی (کم‌امن‌ترین)"))


class ApiSettingsDialog(QDialog):
    def __init__(self, parent, settings: Settings) -> None:
        super().__init__(parent)
        self._settings = settings
        self._test_worker: ConnectionTestWorker | None = None
        self.setWindowTitle(FA["dlg_api_title"])
        self.setMinimumWidth(520)
        self._build_ui()
        self._load()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        api_box = QGroupBox(FA["dlg_api_section_api"])
        api_form = QFormLayout(api_box)

        self.provider_combo = QComboBox()
        for name, cls in available_providers().items():
            self.provider_combo.addItem(cls.display_name, name)
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        api_form.addRow(FA["dlg_api_provider"], self.provider_combo)

        self.base_url_edit = QLineEdit()
        api_form.addRow(FA["dlg_api_base_url"], self.base_url_edit)

        self.model_edit = QLineEdit()
        api_form.addRow(FA["dlg_api_model"], self.model_edit)

        key_row = QHBoxLayout()
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.key_edit.setPlaceholderText(FA["dlg_api_key_hint"])
        key_row.addWidget(self.key_edit)
        delete_key_button = QPushButton(FA["dlg_api_delete_key"])
        delete_key_button.clicked.connect(self._on_delete_key)
        key_row.addWidget(delete_key_button)
        api_form.addRow(FA["dlg_api_key"], key_row)

        self.backend_combo = QComboBox()
        for value, label in _BACKENDS:
            self.backend_combo.addItem(label, value)
        api_form.addRow(FA["dlg_api_backend"], self.backend_combo)

        self.json_mode_check = QCheckBox(FA["dlg_api_json"])
        api_form.addRow(self.json_mode_check)

        self.temperature_spin = QDoubleSpinBox()
        self.temperature_spin.setRange(0.0, 2.0)
        self.temperature_spin.setSingleStep(0.1)
        api_form.addRow(FA["dlg_api_temperature"], self.temperature_spin)

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(10, 600)
        api_form.addRow(FA["dlg_api_timeout"], self.timeout_spin)

        layout.addWidget(api_box)

        translation_box = QGroupBox(FA["dlg_api_section_translation"])
        translation_form = QFormLayout(translation_box)

        self.batch_lines_spin = QSpinBox()
        self.batch_lines_spin.setRange(1, 100)
        translation_form.addRow(FA["dlg_api_batch_lines"], self.batch_lines_spin)

        self.batch_chars_spin = QSpinBox()
        self.batch_chars_spin.setRange(200, 20000)
        self.batch_chars_spin.setSingleStep(100)
        translation_form.addRow(FA["dlg_api_batch_chars"], self.batch_chars_spin)

        self.prev_spin = QSpinBox()
        self.prev_spin.setRange(0, 10)
        translation_form.addRow(FA["dlg_api_prev"], self.prev_spin)

        self.future_spin = QSpinBox()
        self.future_spin.setRange(0, 10)
        translation_form.addRow(FA["dlg_api_future"], self.future_spin)

        self.retries_spin = QSpinBox()
        self.retries_spin.setRange(0, 10)
        translation_form.addRow(FA["dlg_api_retries"], self.retries_spin)

        self.retry_delay_spin = QDoubleSpinBox()
        self.retry_delay_spin.setRange(0.5, 30.0)
        self.retry_delay_spin.setSingleStep(0.5)
        translation_form.addRow(FA["dlg_api_retry_delay"], self.retry_delay_spin)

        self.memory_check = QCheckBox(FA["dlg_api_memory"])
        translation_form.addRow(self.memory_check)

        layout.addWidget(translation_box)

        test_button = QPushButton(FA["dlg_api_test"])
        test_button.clicked.connect(self._on_test_connection)
        layout.addWidget(test_button)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _load(self) -> None:
        api = self._settings.api
        index = self.provider_combo.findData(api.provider)
        self.provider_combo.setCurrentIndex(max(0, index))
        self.base_url_edit.setText(api.base_url)
        self.model_edit.setText(api.model)
        self.key_edit.setText("")
        backend_index = self.backend_combo.findData(api.credential_backend)
        self.backend_combo.setCurrentIndex(max(0, backend_index))
        self.json_mode_check.setChecked(api.json_mode)
        self.temperature_spin.setValue(api.temperature)
        self.timeout_spin.setValue(int(api.timeout_seconds))

        translation = self._settings.translation
        self.batch_lines_spin.setValue(translation.batch_max_lines)
        self.batch_chars_spin.setValue(translation.batch_max_chars)
        self.prev_spin.setValue(translation.context_prev_lines)
        self.future_spin.setValue(translation.context_future_lines)
        self.retries_spin.setValue(translation.max_retries)
        self.retry_delay_spin.setValue(translation.retry_base_delay)
        self.memory_check.setChecked(self._settings.memory.reuse_enabled)

    # ------------------------------------------------------------- handlers
    def _on_provider_changed(self) -> None:
        name = self.provider_combo.currentData()
        offline = name == "offline"
        self.base_url_edit.setEnabled(not offline)
        self.key_edit.setEnabled(not offline)

    def _on_delete_key(self) -> None:
        store = get_store(self.backend_combo.currentData())
        store.delete_key()
        QMessageBox.information(self, FA["dlg_api_title"], "کلید ذخیره‌شده حذف شد.")

    def _on_test_connection(self) -> None:
        try:
            provider = self._build_provider_from_fields()
        except CredentialError as exc:
            QMessageBox.warning(self, FA["dlg_api_test"], str(exc))
            return
        problems = provider.validate_config()
        if problems:
            QMessageBox.warning(self, FA["dlg_api_test"], "\n".join(problems))
            return
        self._test_worker = ConnectionTestWorker(provider, self)
        self._test_worker.done.connect(self._on_test_done)
        self._test_worker.start()

    def _on_test_done(self, ok: bool, message: str) -> None:
        if ok:
            QMessageBox.information(self, FA["dlg_api_test"], message)
        else:
            QMessageBox.warning(self, FA["dlg_api_test"], message)

    # ------------------------------------------------------------- save/quit
    def _build_provider_from_fields(self):
        name = self.provider_combo.currentData() or "openai"
        key = self.key_edit.text().strip()
        if not key:
            key = resolve_api_key(self.backend_combo.currentData())
        return create_provider(
            name,
            base_url=self.base_url_edit.text().strip(),
            api_key=key,
            model=self.model_edit.text().strip(),
            temperature=self.temperature_spin.value(),
            timeout=self.timeout_spin.value(),
        )

    def accept(self) -> None:
        api = self._settings.api
        api.provider = self.provider_combo.currentData() or "openai"
        api.base_url = self.base_url_edit.text().strip()
        api.model = self.model_edit.text().strip()
        api.credential_backend = self.backend_combo.currentData() or "auto"
        api.json_mode = self.json_mode_check.isChecked()
        api.temperature = self.temperature_spin.value()
        api.timeout_seconds = float(self.timeout_spin.value())

        key = self.key_edit.text().strip()
        if key and api.provider != "offline":
            get_store(api.credential_backend).set_key(key)

        translation = self._settings.translation
        translation.batch_max_lines = self.batch_lines_spin.value()
        translation.batch_max_chars = self.batch_chars_spin.value()
        translation.context_prev_lines = self.prev_spin.value()
        translation.context_future_lines = self.future_spin.value()
        translation.max_retries = self.retries_spin.value()
        translation.retry_base_delay = self.retry_delay_spin.value()
        self._settings.memory.reuse_enabled = self.memory_check.isChecked()

        save_settings(self._settings)
        super().accept()
