"""Non-activating dictation overlay and its combined countdown."""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QVBoxLayout, QWidget

from ..config import Settings


class SessionProgress(QWidget):
    """One countdown: yellow expires at recognition, blue at session completion."""

    def __init__(self):
        super().__init__()
        self.setFixedHeight(8)
        self.configure(Settings())

    def configure(self, settings: Settings):
        self.idle_enabled = settings.stop_on_idle
        self.idle_total = settings.idle_timeout
        self.pause_total = settings.recognition_pause
        self.idle_remaining = -1.0
        self.pause_remaining = -1.0
        self.hide()

    def fractions(self) -> tuple[float, float]:
        if self.idle_enabled and self.idle_remaining >= 0:
            remaining = min(self.idle_remaining, self.idle_total)
            blue = remaining
            if self.pause_remaining >= 0:
                yellow = min(max(0, self.pause_remaining), self.pause_total, remaining)
                blue = remaining - yellow
                return blue / self.idle_total, yellow / self.idle_total
            return blue / self.idle_total, 0.0
        if self.pause_remaining >= 0:
            return 0.0, min(self.pause_remaining / self.pause_total, 1.0)
        return 0.0, 0.0

    def refresh(self):
        self.setVisible((self.idle_enabled and self.idle_remaining >= 0) or self.pause_remaining >= 0)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(bounds, 4, 4)
        painter.setClipPath(clip)
        painter.fillRect(bounds, QColor("#2c3b57"))
        blue, yellow = self.fractions()
        width = bounds.width()
        painter.fillRect(QRectF(0, 0, width * blue, bounds.height()), QColor("#4775f5"))
        painter.fillRect(QRectF(width * blue, 0, width * yellow, bounds.height()), QColor("#f6c84a"))


class Overlay(QFrame):
    def __init__(self, position: str = "top_right"):
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        super().__init__(None, flags)
        self._position = position
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedWidth(520)
        self.setStyleSheet("QFrame{background:#182238;border-radius:14px;} QLabel{color:#edf1f9;}")
        layout = QVBoxLayout(self)
        self.state = QLabel("Слушаю · горячая клавиша — завершить")
        self.preview = QLabel("")
        self.preview.setWordWrap(True)
        layout.addWidget(self.state)
        layout.addWidget(self.preview)
        self.timeout_bar = SessionProgress()
        layout.addWidget(self.timeout_bar)
        self.clear_timeout()
        self.resize(520, 95)
        screen = QApplication.primaryScreen()
        if screen is not None:
            screen.availableGeometryChanged.connect(self._place)
        self._place()

    def set_position(self, position: str):
        self._position = position
        self._place()

    def _place(self, *_):
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        margin = 24
        vertical, horizontal = self._position.split("_")
        x = area.left() + margin if horizontal == "left" else area.right() + 1 - self.width() - margin
        y = {
            "top": area.top() + margin,
            "middle": area.top() + (area.height() - self.height()) // 2,
            "bottom": area.bottom() + 1 - self.height() - margin,
        }[vertical]
        x = max(area.left(), min(x, area.right() + 1 - self.width()))
        y = max(area.top(), min(y, area.bottom() + 1 - self.height()))
        self.move(x, y)

    def showEvent(self, event):
        super().showEvent(event)
        self._place()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place()

    def set_timeout(self, remaining: float, total: int):
        self.timeout_bar.idle_total = total
        self.timeout_bar.idle_remaining = max(0, remaining)
        self.timeout_bar.refresh()

    def set_pause(self, remaining: float):
        self.timeout_bar.pause_remaining = remaining
        self.timeout_bar.refresh()

    def set_idle_enabled(self, enabled: bool):
        self.timeout_bar.idle_enabled = enabled
        self.timeout_bar.idle_remaining = self.timeout_bar.idle_total if enabled else -1
        self.timeout_bar.refresh()

    def clear_timeout(self):
        self.timeout_bar.hide()
