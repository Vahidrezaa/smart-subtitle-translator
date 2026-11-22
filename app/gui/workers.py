"""QThread workers bridging core progress events to Qt signals."""
from __future__ import annotations

import logging
import threading
from typing import Callable

from PySide6.QtCore import QThread, Signal

from app.core.batch_processor import BatchProcessor
from app.core.events import LineTranslated, ProgressEvent, RetryScheduled, WarningEvent
from app.core.exceptions import BatchCancelled

log = logging.getLogger(__name__)


class TranslationWorker(QThread):
    progress = Signal(int, int, str)
    line_done = Signal(int, bool)
    retrying = Signal(str)
    warning = Signal(str)
    run_finished = Signal(bool, str)

    def __init__(
        self,
        build_processor: Callable[[Callable[[], bool], Callable[[ProgressEvent], None]], BatchProcessor],
        project_id: int,
        doc,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._build_processor = build_processor
        self._project_id = project_id
        self._doc = doc
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        try:
            processor = self._build_processor(self._cancel_event.is_set, self._on_event)
        except Exception as exc:
            log.exception("Could not build the translation processor")
            self.run_finished.emit(False, f"خطا در آماده‌سازی موتور ترجمه: {exc}")
            return
        try:
            summary = processor.run(self._project_id, self._doc)
            message = (
                f"{summary.translated} خط ترجمه شد، {summary.reused} خط از حافظه استفاده شد، "
                f"{summary.failed} خط ناموفق بود"
            )
            self.run_finished.emit(summary.ok and not summary.failed_positions, message)
        except BatchCancelled:
            self.run_finished.emit(False, "ترجمه متوقف شد؛ پیشرفت ذخیره شده است.")
        except Exception as exc:
            log.exception("Translation run crashed")
            self.run_finished.emit(False, f"خطای غیرمنتظره: {exc}")

    def _on_event(self, event: ProgressEvent) -> None:
        self.progress.emit(event.done, event.total, event.message)
        if isinstance(event, LineTranslated):
            self.line_done.emit(event.position, event.reused)
        elif isinstance(event, RetryScheduled):
            self.retrying.emit(event.message)
        elif isinstance(event, WarningEvent):
            self.warning.emit(event.message)


class ConnectionTestWorker(QThread):
    done = Signal(bool, str)

    def __init__(self, provider, parent=None) -> None:
        super().__init__(parent)
        self._provider = provider

    def run(self) -> None:
        ok, message = self._provider.test_connection()
        self.done.emit(ok, message)


class JobWorker(QThread):
    """Runs any callable off the GUI thread; result/exception via signals."""

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, job: Callable[[], object], parent=None) -> None:
        super().__init__(parent)
        self._job = job

    def run(self) -> None:
        try:
            self.done.emit(self._job())
        except Exception as exc:
            log.exception("Background job failed")
            self.failed.emit(str(exc))
