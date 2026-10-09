"""Ordered system insertion, focus checks and clipboard ownership."""

import sys
import time
from collections import deque

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

from .input import ModifiersHeld, check_focus, is_wayland, modifiers_pressed, paste, windows_unicode


class InsertionQueue(QObject):
    failed = Signal(str, str)
    idle = Signal()

    def __init__(self, target: str, portal=None, mode: str = "auto"):
        super().__init__()
        self.target = target
        self.portal = portal
        self.mode = mode
        self.queue = deque()
        self.busy = False
        self.suspended = False
        self.current = ""
        self.paste_started = 0.0
        self.clipboard_written = False
        self.last_clipboard_text = ""

    def add(self, text: str):
        if self.suspended:
            self.failed.emit("Вставка приостановлена", text)
            return
        self.queue.append(text)
        if not self.busy:
            self._next()

    def _fail(self, error):
        rest = self.current + "".join(self.queue)
        self.queue.clear()
        self.current = ""
        self.busy = False
        self.suspended = True
        self.failed.emit(str(error), rest)
        self.idle.emit()

    def suspend(self) -> str:
        rest = self.current + "".join(self.queue)
        self.queue.clear()
        self.current = ""
        self.busy = False
        self.suspended = True
        self.idle.emit()
        return rest

    def _next(self):
        if self.suspended:
            return
        if not self.queue:
            self.busy = False
            self.current = ""
            self.idle.emit()
            return
        self.busy = True
        self.current = self.queue.popleft()
        try:
            check_focus(self.target)
            if sys.platform == "win32":
                windows_unicode(self.current)
                self.current = ""
                QTimer.singleShot(20, self._next)
                return
            if is_wayland():
                if self.portal is None:
                    raise RuntimeError("В настройках сначала разрешите ввод через портал Wayland")
                self.portal.set_text(self.current)
            else:
                QApplication.clipboard().setText(self.current)
            self.clipboard_written = True
            self.last_clipboard_text = self.current
            # Let Qt serve clipboard requests before the next delta changes ownership.
            self.paste_started = time.monotonic()
            QTimer.singleShot(30, self._paste)
        except Exception as error:
            self._fail(error)

    def _paste(self):
        if self.suspended or not self.current:
            return
        # EXC-0002: approved pre-input modifier wait; see docs/exceptional_execution_paths.md.
        try:
            check_focus(self.target)
            if modifiers_pressed():
                if time.monotonic() - self.paste_started > 5:
                    raise RuntimeError("Отпустите Ctrl/Alt/Shift: текст сохранён для копирования")
                QTimer.singleShot(30, self._paste)
                return
            paste(self.target, self.portal, self.mode)
            self.current = ""
            QTimer.singleShot(150, self._next)
        except ModifiersHeld as error:
            if time.monotonic() - self.paste_started > 5:
                self._fail(error)
            else:
                QTimer.singleShot(30, self._paste)
        except Exception as error:
            self._fail(error)
