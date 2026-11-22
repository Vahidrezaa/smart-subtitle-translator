"""Application entry point: python -m app.main"""
from __future__ import annotations

import logging
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app.config.paths import get_data_dir, get_database_path
from app.config.settings_store import load_settings
from app.database.database_manager import DatabaseManager
from app.gui.main_window import MainWindow


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    settings = load_settings()

    db = DatabaseManager(get_database_path())
    db.migrate()

    app = QApplication(sys.argv)
    app.setLayoutDirection(Qt.RightToLeft)  # Persian-first RTL UI
    app.setStyle("Fusion")

    window = MainWindow(settings, db, data_dir)
    window.show()
    try:
        exit_code = app.exec()
    finally:
        db.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
