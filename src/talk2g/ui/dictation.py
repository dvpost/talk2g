"""Dictation tab: capture status and the confirmed transcript."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLabel, QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout, QWidget


class DictationPage(QWidget):
    toggle_requested = Signal()
    copy_requested = Signal()

    def __init__(self, shortcut: str):
        super().__init__()
        layout = QVBoxLayout(self)
        self.record = QPushButton("Начать диктовку · " + shortcut)
        self.record.setEnabled(False)
        self.record.clicked.connect(self.toggle_requested.emit)
        layout.addWidget(self.record)
        instructions = QLabel(
            "Поставьте курсор в нужное приложение и нажмите горячую клавишу.\n"
            "Сделайте паузу: накопленная речь распознаётся и вводится целым блоком.\n"
            "Горячая клавиша завершает запись и сразу отправляет оставшуюся речь на распознавание."
        )
        instructions.setWordWrap(True)
        layout.addWidget(instructions)
        self.startup_info = QLabel("")
        self.startup_info.setWordWrap(True)
        layout.addWidget(self.startup_info)
        self.recording_info = QLabel("")
        self.recording_info.setWordWrap(True)
        layout.addWidget(self.recording_info)
        self.meter = QProgressBar()
        self.meter.setRange(0, 100)
        self.meter.setTextVisible(False)
        self.meter.setFixedHeight(8)
        layout.addWidget(self.meter)
        layout.addWidget(QLabel("Распознанный текст"))
        self.confirmed = QPlainTextEdit()
        self.confirmed.setReadOnly(True)
        layout.addWidget(self.confirmed)
        copy = QPushButton("Скопировать текст")
        copy.clicked.connect(self.copy_requested.emit)
        layout.addWidget(copy)
