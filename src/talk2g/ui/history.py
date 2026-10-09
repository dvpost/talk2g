"""History tab backed by the bounded local transcript store."""

from PySide6.QtWidgets import (
    QApplication,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..history import History


class HistoryPage(QWidget):
    def __init__(self, history: History):
        super().__init__()
        self.history = history
        layout = QVBoxLayout(self)
        self.history_list = QListWidget()
        self.history_text = QPlainTextEdit()
        self.history_text.setReadOnly(True)
        self.history_list.currentRowChanged.connect(self.select_history)
        layout.addWidget(self.history_list)
        layout.addWidget(self.history_text)
        copy = QPushButton("Скопировать выбранную запись")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.history_text.toPlainText()))
        layout.addWidget(copy)
        clear = QPushButton("Очистить историю")
        clear.clicked.connect(self.clear_history)
        layout.addWidget(clear)
        self.refresh()

    def refresh(self):
        self.entries = self.history.recent()
        self.history_list.clear()
        for at, text in self.entries:
            self.history_list.addItem(at[:16].replace("T", " ") + " · " + text[:85])

    def select_history(self, row):
        self.history_text.setPlainText(self.entries[row][1] if 0 <= row < len(self.entries) else "")

    def clear_history(self):
        if (
            QMessageBox.question(self, "История", "Удалить сохранённые тексты?")
            == QMessageBox.StandardButton.Yes
        ):
            self.history.clear()
            self.refresh()
