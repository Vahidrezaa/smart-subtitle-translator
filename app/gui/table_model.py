"""Table model for the subtitle list (original + Persian columns)."""
from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from app.core.subtitle_model import EntryStatus, SubtitleDocument, ms_to_srt_time

_HEADERS = ("#", None, None, None, None)  # replaced at runtime with FA strings

_STATUS_BG = {
    EntryStatus.MACHINE: QColor(0xE8, 0xF5, 0xE9),
    EntryStatus.CORRECTED: QColor(0xE3, 0xF2, 0xFD),
    EntryStatus.APPROVED: QColor(0xC8, 0xE6, 0xC9),
    EntryStatus.FAILED: QColor(0xFF, 0xEB, 0xEE),
    EntryStatus.SKIPPED: QColor(0xF5, 0xF5, 0xF5),
}


class SubtitlesTableModel(QAbstractTableModel):
    SEQ, START, END, ORIGINAL, PERSIAN = range(5)

    def __init__(self, doc: SubtitleDocument | None = None, headers: tuple[str, ...] | None = None) -> None:
        super().__init__()
        self._doc = doc
        self._headers = headers or ("#", "start", "end", "original", "persian")

    @property
    def doc(self) -> SubtitleDocument | None:
        return self._doc

    def set_document(self, doc: SubtitleDocument | None) -> None:
        self.beginResetModel()
        self._doc = doc
        self.endResetModel()

    def refresh_entry(self, position: int) -> None:
        if self._doc is None:
            return
        for column in (self.ORIGINAL, self.PERSIAN):
            index = self.index(position, column)
            self.dataChanged.emit(index, index)

    def refresh_all(self) -> None:
        if self._doc is None or not self._doc.entries:
            return
        top_left = self.index(0, 0)
        bottom_right = self.index(len(self._doc) - 1, self.PERSIAN)
        self.dataChanged.emit(top_left, bottom_right)

    # ------------------------------------------------------------ Qt plumbing
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid() or self._doc is None:
            return 0
        return len(self._doc)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 5

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return self._headers[section] if section < len(self._headers) else None
        return section + 1

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if self._doc is None or not index.isValid():
            return None
        entry = self._doc.get(index.row())
        column = index.column()

        if role == Qt.DisplayRole:
            if column == self.SEQ:
                return entry.sequence
            if column == self.START:
                return ms_to_srt_time(entry.start_ms)
            if column == self.END:
                return ms_to_srt_time(entry.end_ms)
            if column == self.ORIGINAL:
                return entry.working_text
            if column == self.PERSIAN:
                return entry.translated_text or ""
            return None

        if role == Qt.ToolTipRole:
            if column == self.ORIGINAL and entry.working_text != entry.original_text:
                return f"متن پس از اصلاح — اصل: {entry.original_text}"
            if column == self.PERSIAN and entry.status is EntryStatus.FAILED:
                return "ترجمه این خط ناموفق بود؛ از دکمه تلاش مجدد استفاده کنید."
            return None

        if role == Qt.BackgroundRole:
            return _STATUS_BG.get(entry.status)

        if role == Qt.TextAlignmentRole:
            if column in (self.SEQ, self.START, self.END):
                return int(Qt.AlignCenter | Qt.AlignVCenter)
            if column == self.ORIGINAL:
                # English text reads LTR even in the RTL layout
                return int(Qt.AlignAbsolute | Qt.AlignLeft | Qt.AlignVCenter)
            return int(Qt.AlignRight | Qt.AlignVCenter)

        if role == Qt.EditRole and column == self.PERSIAN:
            return entry.translated_text or ""
        return None

    def flags(self, index: QModelIndex):
        flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if self._doc is not None and index.column() == self.PERSIAN:
            entry = self._doc.get(index.row())
            if entry.status is not EntryStatus.SKIPPED:
                flags |= Qt.ItemIsEditable
        return flags

    def setData(self, index: QModelIndex, value, role: int = Qt.EditRole) -> bool:
        if role != Qt.EditRole or self._doc is None:
            return False
        if index.column() != self.PERSIAN:
            return False
        text = str(value).strip()
        if not text:
            return False
        entry = self._doc.get(index.row())
        entry.translated_text = text
        entry.status = EntryStatus.CORRECTED  # manual edit counts as corrected
        self.dataChanged.emit(index, index)
        return True
