from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from collections import deque
from dataclasses import replace
from urllib.parse import urlsplit

from PySide6.QtCore import QMimeData, QObject, QRectF, QSignalBlocker, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .autostart import Autostart
from .client import Delivery, DictationThread
from .config import Settings
from .history import History
from .hotkey import NativeHotkey, parse_hotkey
from .input import (
    ModifiersHeld,
    check_focus,
    foreground,
    is_wayland,
    modifiers_pressed,
    paste,
    windows_unicode,
)
from .recording import recordings_directory
from .service import LocalService, health
from .voice_command import VoiceStop

log = logging.getLogger(__name__)


def socket_name() -> str:
    return "talk2g-" + (str(os.getuid()) if hasattr(os, "getuid") else os.environ.get("USERNAME", "user"))


def hotkey_label(value: str) -> str:
    return "+".join(part.strip("<>").title() for part in value.split("+"))


def send_control(command: str) -> bool:
    app = QApplication.instance() or QApplication([])
    socket = QLocalSocket()
    socket.connectToServer(socket_name())
    if not socket.waitForConnected(500):
        return False
    socket.write(command.encode())
    socket.waitForBytesWritten(500)
    socket.disconnectFromServer()
    del app
    return True


def app_icon() -> QIcon:
    canvas = QPixmap(64, 64)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#4775f5"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(2, 2, 60, 60, 16, 16)
    painter.setPen(QColor("white"))
    painter.setFont(QFont("Sans", 29, QFont.Weight.Bold))
    painter.drawText(canvas.rect(), Qt.AlignmentFlag.AlignCenter, "t")
    painter.end()
    return QIcon(canvas)


class Bridge(QObject):
    toggle = Signal()
    health_result = Signal(object)
    permission_result = Signal(str)


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
            if self.portal and self.portal.set_text(self.current):
                pass
            else:
                if is_wayland():
                    import shutil
                    import subprocess

                    if not shutil.which("wl-copy"):
                        raise RuntimeError("Портал не поддерживает clipboard; установите wl-clipboard")
                    subprocess.run(
                        ["wl-copy", "--type", "text/plain;charset=utf-8"],
                        input=self.current.encode(),
                        check=True,
                        timeout=3,
                    )
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


class SessionProgress(QWidget):
    """One countdown: yellow expires at recognition, blue at session completion."""

    def __init__(self):
        super().__init__()
        self.setFixedHeight(8)
        self.configure(Settings())

    def configure(self, settings: Settings):
        self.idle_enabled = settings.stop_on_idle
        self.pause_enabled = settings.recognize_on_pause
        self.idle_total = settings.idle_timeout
        self.pause_total = settings.recognition_pause
        self.idle_remaining = -1.0
        self.pause_remaining = -1.0
        self.hide()

    def fractions(self) -> tuple[float, float]:
        if self.idle_enabled and self.idle_remaining >= 0:
            remaining = min(self.idle_remaining, self.idle_total)
            blue = remaining
            if self.pause_enabled and self.pause_remaining >= 0:
                yellow = min(max(0, self.pause_remaining), self.pause_total, remaining)
                blue = remaining - yellow
                return blue / self.idle_total, yellow / self.idle_total
            return blue / self.idle_total, 0.0
        if self.pause_enabled and self.pause_remaining >= 0:
            return 0.0, min(self.pause_remaining / self.pause_total, 1.0)
        return 0.0, 0.0

    def value(self) -> int:
        return round(1000 * sum(self.fractions()))

    def refresh(self):
        self.setVisible(
            (self.idle_enabled and self.idle_remaining >= 0)
            or (self.pause_enabled and self.pause_remaining >= 0)
        )
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


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings, *, start_service: bool = True):
        super().__init__()
        self.autostart = Autostart()
        try:
            settings = replace(settings, autostart=self.autostart.is_enabled())
        except OSError as error:
            log.warning("Не удалось проверить автозапуск: %s", error)
        self.settings = settings
        self.service = LocalService(settings)
        self.history = History()
        self.thread: DictationThread | None = None
        self.delivery = Delivery()
        self.raw_delivery = Delivery()
        self.voice_stop = VoiceStop()
        self.inserter = None
        self.remainder = ""
        self.ready = False
        self.checking = False
        self.quitting = False
        self.last_error = ""
        self.start_pending = False
        self.activated_at = 0.0
        self.clipboard_pending = False
        self.clipboard_before = None
        self.insertion_queues = []
        self.hotkey = None
        self.portal = None
        self.bridge = Bridge()
        self.bridge.toggle.connect(self.toggle)
        self.bridge.health_result.connect(self._health_result)
        self.bridge.permission_result.connect(self._permission_result)
        self.overlay = Overlay(self.settings.overlay_position)
        self.setWindowTitle("talk2g")
        self.setWindowIcon(app_icon())
        self.resize(740, 700)
        self.setStyleSheet(
            "QMainWindow{background:#f5f7fc;} QPushButton{padding:9px;}"
            "QPlainTextEdit{background:white;border:1px solid #d2d9e7;border-radius:6px;}"
            "QTabWidget::pane{border:0;}"
        )
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        heading = QLabel("talk2g")
        heading.setFont(QFont("Sans", 24, QFont.Weight.Bold))
        layout.addWidget(heading)
        self.status = QLabel("Подготовка локальной модели…")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self._build_dictation()
        self._build_settings()
        self._build_history()
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        menu = QMenu()
        toggle_action = QAction("Начать / остановить диктовку", self)
        toggle_action.triggered.connect(self.toggle)
        menu.addAction(toggle_action)
        show_action = QAction("Открыть приложение", self)
        show_action.triggered.connect(self.show_normal)
        menu.addAction(show_action)
        menu.addSeparator()
        self.feature_actions = {}
        for name, label in (
            ("auto_insert", "Автоматически вводить текст"),
            ("copy_on_stop", "Копировать диктовку в буфер обмена"),
            ("show_overlay", "Показывать синее окно диктовки"),
            ("load_on_demand", "Загружать модель только при диктовке"),
            ("autostart", "Запускать при входе в систему"),
            ("stop_on_phrase", "Останавливать по фразе «конец связи»"),
            ("stop_on_idle", "Автозавершение без речи"),
            ("recognize_on_pause", "Распознавать после паузы"),
            ("save_recordings", "Сохранять аудиозаписи для разбора ошибок"),
        ):
            action = QAction(label, self)
            action.setCheckable(True)
            action.setChecked(getattr(self.settings, name))
            action.toggled.connect(lambda checked, name=name: self.set_feature(name, checked))
            self.feature_actions[name] = action
            menu.addAction(action)
        menu.addSeparator()
        quit_action = QAction("Выход", self)
        quit_action.triggered.connect(self.quit)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.setToolTip("talk2g · " + hotkey_label(self.settings.hotkey))
        self.tray.activated.connect(
            lambda reason: self.show_normal() if reason == QSystemTrayIcon.ActivationReason.Trigger else None
        )
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        self.control = QLocalServer(self)
        QLocalServer.removeServer(socket_name())
        if not self.control.listen(socket_name()):
            raise RuntimeError("Не удалось открыть локальное управление приложением")
        self.control.newConnection.connect(self._control_message)
        self._install_hotkey()
        self._sync_feature_controls()
        if start_service and not settings.load_on_demand:
            self.service.start()
        self.health_timer = QTimer(self)
        self.health_timer.timeout.connect(self.check_server)
        self.health_timer.start(2000)
        if settings.load_on_demand:
            self._idle_on_demand()
        elif start_service:
            self.check_server()

    def _build_dictation(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.record = QPushButton("Начать диктовку · " + hotkey_label(self.settings.hotkey))
        self.record.setEnabled(False)
        self.record.clicked.connect(self.toggle)
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
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.delivery.text))
        layout.addWidget(copy)
        self.tabs.addTab(widget, "Диктовка")

    def _build_settings(self):
        widget = QWidget()
        form = QFormLayout(widget)
        self.url = QLineEdit(self.settings.server_url)
        form.addRow("Сервер", self.url)
        self.token = QLineEdit(self.settings.token)
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Токен удалённого сервера", self.token)
        self.microphone = QComboBox()
        self.microphone.addItem("Системный микрофон по умолчанию", "")
        try:
            import sounddevice as sd

            names = list(dict.fromkeys(d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0))
            for name in names:
                self.microphone.addItem(name, name)
        except Exception as error:
            self.status.setText(f"Микрофон: {error}")
        index = self.microphone.findData(self.settings.microphone)
        if index >= 0:
            self.microphone.setCurrentIndex(index)
        form.addRow("Микрофон", self.microphone)
        self.hotkey_field = QLineEdit(self.settings.hotkey)
        form.addRow("Хоткей (Windows/X11)", self.hotkey_field)
        self.automatic = QCheckBox("Постепенно вводить текст в активное приложение")
        self.automatic.setChecked(self.settings.auto_insert)
        form.addRow(self.automatic)
        self.paste_method = QComboBox()
        for label, value in (
            ("Автоматически: терминал / обычное поле", "auto"),
            ("Ctrl+V", "ctrl_v"),
            ("Ctrl+Shift+V", "ctrl_shift_v"),
            ("Shift+Insert", "shift_insert"),
        ):
            self.paste_method.addItem(label, value)
        self.paste_method.setCurrentIndex(self.paste_method.findData(self.settings.paste_mode))
        form.addRow("Вставка в Linux", self.paste_method)
        self.copy_final = QCheckBox("После завершения копировать весь текст в буфер обмена")
        self.copy_final.setChecked(self.settings.copy_on_stop)
        form.addRow(self.copy_final)
        self.overlay_option = QCheckBox("Показывать синее окно с распознанным текстом")
        self.overlay_option.setChecked(self.settings.show_overlay)
        form.addRow(self.overlay_option)
        self.overlay_position_field = QComboBox()
        for label, value in (
            ("Справа сверху", "top_right"),
            ("Справа посередине", "middle_right"),
            ("Справа снизу", "bottom_right"),
            ("Слева сверху", "top_left"),
            ("Слева посередине", "middle_left"),
            ("Слева снизу", "bottom_left"),
        ):
            self.overlay_position_field.addItem(label, value)
        self.overlay_position_field.setCurrentIndex(
            self.overlay_position_field.findData(self.settings.overlay_position)
        )
        self.overlay_position_field.setToolTip("Применяется сразу и сохраняется между запусками.")
        self.overlay_position_field.currentIndexChanged.connect(self.set_overlay_position)
        form.addRow("Положение окна диктовки", self.overlay_position_field)
        self.demand_option = QCheckBox("Загружать модель только при диктовке")
        self.demand_option.setChecked(self.settings.load_on_demand)
        form.addRow(self.demand_option)
        model_note = QLabel(
            "С галочкой: запись начинается сразу, модель загружается параллельно "
            "и выгружается после остановки.\n"
            "Без галочки: модель постоянно в памяти, распознавание начинается быстрее."
        )
        model_note.setWordWrap(True)
        form.addRow(model_note)
        self.autostart_option = QCheckBox("Запускать при входе в систему")
        self.autostart_option.setChecked(self.settings.autostart)
        form.addRow(self.autostart_option)
        self.voice_stop_option = QCheckBox("Останавливать по фразе «конец связи»")
        self.voice_stop_option.setChecked(self.settings.stop_on_phrase)
        self.voice_stop_option.setToolTip(
            "Произнесите команду и сделайте короткую паузу. Команда завершает диктовку и не попадает в текст."
        )
        form.addRow(self.voice_stop_option)
        self.idle_stop_option = QCheckBox("Автозавершение без речи")
        self.idle_stop_option.setChecked(self.settings.stop_on_idle)
        form.addRow(self.idle_stop_option)
        self.pause_option = QCheckBox("Распознавать после паузы")
        self.pause_option.setChecked(self.settings.recognize_on_pause)
        self.pause_option.setToolTip(
            "Распознаёт накопленную речь один раз после паузы. "
            "Новая речь сбрасывает ожидание. Переключение применяется к следующей диктовке."
        )
        form.addRow(self.pause_option)
        self.save_recordings_option = QCheckBox("Сохранять аудиозаписи для разбора ошибок")
        self.save_recordings_option.setChecked(self.settings.save_recordings)
        self.save_recordings_option.setToolTip(
            "Сохраняет полученный сервером WAV и журнал распознавания. "
            "Изменение применяется к следующей диктовке. По умолчанию выключено."
        )
        form.addRow(self.save_recordings_option)
        self.open_recordings_button = QPushButton("Открыть папку записей")
        self.open_recordings_button.clicked.connect(self.open_recordings)
        form.addRow(self.open_recordings_button)
        for checkbox, name in (
            (self.automatic, "auto_insert"),
            (self.copy_final, "copy_on_stop"),
            (self.overlay_option, "show_overlay"),
            (self.demand_option, "load_on_demand"),
            (self.autostart_option, "autostart"),
            (self.voice_stop_option, "stop_on_phrase"),
            (self.idle_stop_option, "stop_on_idle"),
            (self.pause_option, "recognize_on_pause"),
            (self.save_recordings_option, "save_recordings"),
        ):
            checkbox.toggled.connect(lambda checked, name=name: self.set_feature(name, checked))
        self.idle_timeout_field = QSpinBox()
        self.idle_timeout_field.setRange(1, 7200)
        self.idle_timeout_field.setSuffix(" с")
        self.idle_timeout_field.setValue(self.settings.idle_timeout)
        self.idle_timeout_field.setEnabled(self.settings.stop_on_idle)
        self.idle_timeout_field.setToolTip(
            "Диктовка завершается после паузы без речи. Новая речь сбрасывает отсчёт. "
            "По умолчанию — 45 секунд."
        )
        form.addRow("Тайм-аут без речи", self.idle_timeout_field)
        self.recognition_pause_field = QDoubleSpinBox()
        self.recognition_pause_field.setRange(0.5, 10)
        self.recognition_pause_field.setSingleStep(0.5)
        self.recognition_pause_field.setSuffix(" с")
        self.recognition_pause_field.setValue(self.settings.recognition_pause)
        self.recognition_pause_field.setEnabled(self.settings.recognize_on_pause)
        self.recognition_pause_field.setToolTip(
            "Жёлтая часть полосы отсчитывает паузу до распознавания целого блока. По умолчанию — 3 секунды."
        )
        form.addRow("Пауза до распознавания", self.recognition_pause_field)
        save = QPushButton("Сохранить настройки")
        save.clicked.connect(self.save_settings)
        form.addRow(save)
        if is_wayland():
            allow = QPushButton("Разрешить ввод и хоткей Wayland")
            allow.clicked.connect(self.allow_wayland)
            form.addRow(allow)
            note = QLabel("Wayland вводит текст в текущее поле. Во время диктовки оставляйте фокус в нём.")
            note.setWordWrap(True)
            form.addRow(note)
        note = QLabel(
            "Модель: GigaAM v3 E2E · CPU INT8. После загрузки работает офлайн.\n"
            "Переключатели выше также доступны по правой кнопке на значке «t» в трее."
        )
        note.setWordWrap(True)
        form.addRow(note)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(widget)
        self.tabs.addTab(scroll, "Настройки")

    def open_recordings(self):
        directory = recordings_directory()
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory))):
                raise OSError("Не удалось открыть файловый менеджер")
        except OSError as error:
            self.status.setText(f"Папка записей: {error}")

    def _build_history(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
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
        self.tabs.addTab(widget, "История")
        self.refresh_history()

    def refresh_history(self):
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
            self.refresh_history()

    def _install_hotkey(self):
        if self.hotkey:
            self.hotkey.stop()
            self.hotkey = None
        if is_wayland():
            return
        if self.last_error.startswith("Хоткей недоступен:"):
            self.last_error = ""
        try:
            self.hotkey = NativeHotkey(self.settings.hotkey, self.bridge.toggle.emit)
            self.hotkey.start()
        except Exception as error:
            self.last_error = f"Хоткей недоступен: {error}. Используйте кнопку записи"
            self.status.setText(self.last_error)

    def _control_message(self):
        socket = self.control.nextPendingConnection()

        def reading():
            command = bytes(socket.readAll()).decode()
            if command == "toggle":
                self.toggle()
            elif command == "quit":
                self.quit()
            elif command == "ping":
                pass
            else:
                self.show_normal()
            socket.disconnectFromServer()
            socket.deleteLater()

        socket.readyRead.connect(reading)
        if socket.bytesAvailable():
            reading()

    def check_server(self):
        if self.checking or self.quitting:
            return
        if self.settings.load_on_demand and not self.thread:
            return
        self.checking = True

        def checking():
            try:
                result = health(self.settings.server_url)
            except Exception as error:
                result = {"ready": False, "error": str(error)}
            self.bridge.health_result.emit(result)

        threading.Thread(target=checking, daemon=True).start()

    def _health_result(self, result):
        self.checking = False
        if self.settings.load_on_demand and not self.thread:
            return  # Ignore an in-flight readiness poll after unloading the process.
        self.ready = bool(result.get("ready"))
        if self.thread:
            return
        if self.start_pending and self.ready:
            self._begin_requested_recording()
            return
        self.record.setEnabled(self.ready)
        if self.ready:
            if self.last_error:
                self.status.setText(self.last_error)
                return
            self.status.setText(
                "Готово · GigaAM работает локально"
                if "127.0.0.1" in self.settings.server_url
                else "Готово · сервер подключён"
            )
        elif result.get("error"):
            self.status.setText("Сервер недоступен: " + result["error"] + " · подробности: .data/server.log")
        else:
            self.status.setText(
                "Подготовка GigaAM… Первый запуск скачивает модель, следующие работают офлайн"
            )

    def save_settings(self):
        if self.thread:
            QMessageBox.information(self, "Настройки", "Завершите текущую диктовку")
            return
        try:
            new = replace(
                self.settings,
                server_url=self.url.text().strip(),
                token=self.token.text().strip(),
                microphone=self.microphone.currentData(),
                hotkey=self.hotkey_field.text().strip(),
                auto_insert=self.automatic.isChecked(),
                paste_mode=self.paste_method.currentData(),
                copy_on_stop=self.copy_final.isChecked(),
                show_overlay=self.overlay_option.isChecked(),
                overlay_position=self.overlay_position_field.currentData(),
                load_on_demand=self.demand_option.isChecked(),
                autostart=self.autostart_option.isChecked(),
                stop_on_phrase=self.voice_stop_option.isChecked(),
                stop_on_idle=self.idle_stop_option.isChecked(),
                idle_timeout=self.idle_timeout_field.value(),
                save_recordings=self.save_recordings_option.isChecked(),
                recognize_on_pause=self.pause_option.isChecked(),
                recognition_pause=self.recognition_pause_field.value(),
            )
            new.validate()
            if not is_wayland():
                parse_hotkey(new.hotkey)
            new.save()
            restart_server = any(
                getattr(new, name) != getattr(self.settings, name)
                for name in ("server_url", "model", "threads")
            )
            hotkey_changed = new.hotkey != self.settings.hotkey
            self.settings = new
            self._sync_feature_controls()
            self.record.setText("Начать диктовку · " + hotkey_label(new.hotkey))
            self.tray.setToolTip("talk2g · " + hotkey_label(new.hotkey))
            if restart_server:
                self.service.close()
                self.ready = False
                self.service = LocalService(new)
                if new.load_on_demand:
                    self._idle_on_demand()
                else:
                    self.service.start()
            if hotkey_changed:
                self._install_hotkey()
            self.check_server()
        except Exception as error:
            QMessageBox.warning(self, "Настройки", str(error))

    def allow_wayland(self):
        from .portal import WaylandPortal

        if self.portal is None:
            self.portal = WaylandPortal(self.bridge.toggle.emit)

        def allowing():
            try:
                text = self.portal.authorize()
            except Exception as error:
                text = f"Wayland: {error}"
            self.bridge.permission_result.emit(text)

        threading.Thread(target=allowing, daemon=True).start()

    def _permission_result(self, text):
        QMessageBox.information(self, "Wayland", text)

    def _sync_feature_controls(self):
        for name, checkbox in (
            ("auto_insert", self.automatic),
            ("copy_on_stop", self.copy_final),
            ("show_overlay", self.overlay_option),
            ("load_on_demand", self.demand_option),
            ("autostart", self.autostart_option),
            ("stop_on_phrase", self.voice_stop_option),
            ("stop_on_idle", self.idle_stop_option),
            ("recognize_on_pause", self.pause_option),
            ("save_recordings", self.save_recordings_option),
        ):
            for control in (checkbox, self.feature_actions[name]):
                with QSignalBlocker(control):
                    control.setChecked(getattr(self.settings, name))
        self.idle_timeout_field.setEnabled(self.settings.stop_on_idle)
        self.recognition_pause_field.setEnabled(self.settings.recognize_on_pause)
        with QSignalBlocker(self.overlay_position_field):
            self.overlay_position_field.setCurrentIndex(
                self.overlay_position_field.findData(self.settings.overlay_position)
            )
        self.overlay.set_position(self.settings.overlay_position)
        local = urlsplit(self.settings.server_url).hostname in ("127.0.0.1", "localhost", "::1")
        self.open_recordings_button.setEnabled(local)
        self.open_recordings_button.setToolTip(
            str(recordings_directory()) if local else "Записи сохраняются на компьютере сервера распознавания"
        )

    def set_overlay_position(self):
        position = self.overlay_position_field.currentData()
        if position == self.settings.overlay_position:
            return
        try:
            new = replace(self.settings, overlay_position=position)
            new.save()
            self.settings = new
            self._sync_feature_controls()
        except Exception as error:
            self._sync_feature_controls()
            self.status.setText(f"Не удалось изменить положение окна диктовки: {error}")

    def set_feature(self, name: str, enabled: bool):
        if getattr(self.settings, name) == enabled:
            return
        try:
            new = replace(self.settings, **{name: enabled})
            if name == "autostart":
                self.autostart.set_enabled(enabled)
                try:
                    new.save()
                except Exception:
                    self.autostart.set_enabled(not enabled)
                    raise
            else:
                new.save()
            self.settings = new
            self._sync_feature_controls()
            if name == "show_overlay":
                self._sync_overlay()
            elif name == "stop_on_idle":
                if self.thread:
                    self.thread.set_idle_enabled(enabled)
                    if self.record.isEnabled():
                        self.overlay.set_idle_enabled(enabled)
            elif name == "stop_on_phrase" and self.thread:
                self._voice_delta("")  # disabling releases a held ordinary word immediately
            elif name == "auto_insert":
                if enabled and self.thread:
                    self._create_inserter(foreground())
                elif not enabled and self.inserter:
                    self.remainder += self.inserter.suspend()
            elif name == "load_on_demand" and not self.thread:
                if enabled:
                    self.service.close()
                    self._idle_on_demand()
                    if self.start_pending:
                        self._begin_requested_recording()
                else:
                    self.ready = False
                    self.record.setEnabled(False)
                    self.status.setText("Подготовка локальной модели…")
                    self.service.start()
                    self.check_server()
        except Exception as error:
            self._sync_feature_controls()
            self.status.setText(f"Не удалось изменить настройку: {error}")

    def _sync_overlay(self):
        if self.settings.show_overlay and (self.thread or self.start_pending):
            self.overlay.show()
        else:
            self.overlay.hide()

    def _create_inserter(self, target):
        self.inserter = InsertionQueue(target, self.portal, self.settings.paste_mode)
        self.inserter.failed.connect(self._insertion_failure)
        self.inserter.idle.connect(self._copy_completed_text)
        self.insertion_queues.append(self.inserter)

    def _idle_on_demand(self):
        self.ready = False
        self.record.setEnabled(True)
        self.status.setText("Готово · модель загрузится по горячей клавише")

    def _begin_requested_recording(self):
        if not self.start_pending or self.quitting:
            return
        if not self.ready and not self.settings.load_on_demand:
            self.check_server()
            return
        if self.inserter and self.inserter.busy:
            QTimer.singleShot(30, self._begin_requested_recording)
            return
        self.start_recording()

    def toggle(self):
        if self.quitting:
            return
        log.info("Хоткей: запись=%s, запуск=%s, готово=%s", bool(self.thread), self.start_pending, self.ready)
        if self.thread:
            self.stop_recording()
            return
        if self.start_pending:
            self.start_pending = False
            self.status.setText("Запуск диктовки отменён")
            self._sync_overlay()
            return
        if is_wayland() and self.settings.auto_insert and (not self.portal or not self.portal.session):
            self.show_normal()
            self.tabs.setCurrentIndex(1)
            QMessageBox.information(self, "Wayland", "Сначала разрешите ввод в настройках приложения")
            return
        if self.settings.auto_insert:
            self.hide()
        self.start_pending = True
        self.activated_at = time.monotonic()
        self.status.setText(
            "Запускаю диктовку…"
            if self.ready or self.settings.load_on_demand
            else "Модель готовится · диктовка начнётся автоматически"
        )
        self.overlay.state.setText(self.status.text())
        self.overlay.preview.clear()
        self.overlay.clear_timeout()
        self._sync_overlay()
        QTimer.singleShot(250, self._begin_requested_recording)

    def stop_recording(self):
        if self.thread and self.record.isEnabled():
            self.thread.stop()
            self.overlay.clear_timeout()
            self.status.setText("Завершаю оставшийся хвост…")
            self.overlay.state.setText("Завершаю оставшийся хвост…")
            self.record.setEnabled(False)

    def _idle_remaining(self, remaining):
        if (
            self.thread
            and self.sender() is self.thread
            and self.settings.stop_on_idle
            and not self.quitting
            and self.record.isEnabled()
        ):
            self.overlay.set_timeout(remaining, self.settings.idle_timeout)

    def _idle_expired(self):
        if self.thread and self.sender() is self.thread and self.settings.stop_on_idle and not self.quitting:
            log.info("Диктовка остановлена после %s секунд без речи", self.settings.idle_timeout)
            self.stop_recording()
            message = f"Пауза {self.settings.idle_timeout} с · завершаю оставшийся хвост…"
            self.status.setText(message)
            self.overlay.state.setText(message)

    def _pause_remaining(self, remaining):
        if (
            self.thread
            and self.sender() is self.thread
            and self.thread.settings.recognize_on_pause
            and not self.quitting
            and self.record.isEnabled()
        ):
            self.overlay.set_pause(remaining)

    def start_recording(self, *, audio_file: str | None = None):
        self.start_pending = False
        if self.quitting or self.thread:
            return
        try:
            target = foreground() if self.settings.auto_insert else ""
        except Exception as error:
            self._failure(str(error))
            return
        self.delivery = Delivery()
        self.raw_delivery = Delivery()
        self.voice_stop = VoiceStop()
        self.clipboard_pending = False
        self.clipboard_before = QMimeData()
        original = QApplication.clipboard().mimeData()
        if original is not None:
            # Request content through Qt's MIME conversion. Native X11 targets
            # (TARGETS, TIMESTAMP, SAVE_TARGETS...) are protocol operations, not
            # payloads, and asking for their data can block until Qt times out.
            if original.hasText():
                self.clipboard_before.setText(original.text())
            if original.hasHtml():
                self.clipboard_before.setHtml(original.html())
            if original.hasUrls():
                self.clipboard_before.setUrls(original.urls())
            if original.hasImage():
                self.clipboard_before.setImageData(original.imageData())
        self.insertion_queues = []
        self.last_error = ""
        self.remainder = ""
        self.confirmed.clear()
        self._create_inserter(target)
        self.startup_info.clear()
        self.recording_info.clear()
        self.thread = DictationThread(
            self.settings, audio_file=audio_file, activated_at=self.activated_at or time.monotonic()
        )
        self.thread.connected.connect(lambda: self.status.setText("Слушаю…"))
        self.thread.event.connect(self._event)
        self.thread.failure.connect(self._failure)
        self.thread.level.connect(lambda value: self.meter.setValue(min(100, int(value * 500))))
        self.thread.idle_remaining.connect(self._idle_remaining)
        self.thread.idle_expired.connect(self._idle_expired)
        self.thread.pause_remaining.connect(self._pause_remaining)
        self.thread.finished.connect(self._finished)
        self.status.setText("Подключаю микрофон…")
        self.record.setText("Остановить диктовку")
        self.record.setEnabled(True)
        self.overlay.state.setText("Слушаю · горячая клавиша — завершить")
        self.overlay.preview.setText("")
        self.overlay.timeout_bar.configure(self.settings)
        self.overlay.clear_timeout()
        self._sync_overlay()
        self.tray.setToolTip("talk2g · слушаю · " + hotkey_label(self.settings.hotkey))
        self.thread.start()
        if self.settings.load_on_demand:
            try:
                self.service.start()
            except Exception as error:
                self._failure(str(error))
                self.thread.cancel()

    def _event(self, event):
        if self.quitting:
            return
        if event["type"] == "recording":
            if event["status"] == "error":
                self.recording_info.setText("Не удалось сохранить аудио: " + event["message"])
                log.warning("Сохранение аудио: %s", event["message"])
            else:
                label = "Аудиозапись сохранена" if event["status"] == "saved" else "Сохранение аудио"
                self.recording_info.setText(f"{label}: {event['path']}")
        elif event["type"] == "loading":
            state = "Записываю" if event["capturing"] else "Запись остановлена"
            message = (
                f"{state} · модель загружается {event['seconds']:.1f} с · "
                f"сохранено {event['buffered_audio_seconds']:.1f} с речи"
            )
            self.status.setText(message)
            self.overlay.state.setText(message)
        elif event["type"] == "model_ready":
            log.info("Модель готова: %s", json.dumps(event))
            self.ready = True
            self.startup_info.setText(
                f"Подготовка распознавания: {event['activation_seconds']:.2f} с от нажатия · "
                f"накоплено {event['buffered_audio_seconds']:.1f} с записи"
            )
            message = (
                "Слушаю · распознаю накопленную речь"
                if event["capturing"]
                else "Распознаю сохранённую запись…"
            )
            self.status.setText(message)
            self.overlay.state.setText(message)
        elif event["type"] == "commit":
            try:
                delta = self.raw_delivery.accept(event)
            except Exception as error:
                self._failure(str(error))
                self.thread.cancel()
                return
            self._voice_delta(delta)
        elif event["type"] == "recognizing":
            message = (
                "Распознаю блок · запись продолжается"
                if self.record.isEnabled()
                else "Распознаю оставшийся блок…"
            )
            self.status.setText(message)
            self.overlay.state.setText(message)
        elif event["type"] == "partial":
            if self.voice_stop.triggered:
                return
            self.overlay.preview.setText(self.delivery.text[-130:] + "  " + event["text"])
        elif event["type"] == "segment_end":
            self.overlay.preview.setText(self.delivery.text[-200:])
            if self.thread and self.record.isEnabled():
                self.status.setText("Слушаю…")
                self.overlay.state.setText("Слушаю · горячая клавиша — завершить")
        elif event["type"] == "session_end":
            self._voice_delta("", final=True)
            if event["text"] != self.raw_delivery.text:
                self._failure("Итог сервера отличается от полученных фрагментов. Текст сохранён в истории")
                # Preserve the server's complete result without inserting it a second time.
                clean, _ = VoiceStop().feed(
                    event["text"],
                    enabled=self.settings.stop_on_phrase or self.voice_stop.triggered,
                    final=True,
                )
                self.delivery.text = clean
                self.confirmed.setPlainText(clean)

    def _voice_delta(self, delta, *, final=False):
        clean, stop = self.voice_stop.feed(delta, enabled=self.settings.stop_on_phrase, final=final)
        self.delivery.text += clean
        self.confirmed.setPlainText(self.delivery.text)
        bar = self.confirmed.verticalScrollBar()
        bar.setValue(bar.maximum())
        if clean:
            if self.settings.auto_insert:
                self.inserter.add(clean)
            else:
                self.remainder += clean
        self.overlay.preview.setText(self.delivery.text[-200:])
        if stop:
            log.info("Диктовка остановлена голосовой командой")
            self.stop_recording()

    def _insertion_failure(self, message, text):
        log.warning("Вставка приостановлена: %s; символов осталось: %d", message, len(text))
        self.remainder += text
        self.last_error = message
        self.status.setText(message)
        self.overlay.state.setText("Вставка приостановлена · текст сохраняется")

    def _failure(self, message):
        self.last_error = "Ошибка: " + message
        self.status.setText("Ошибка: " + message)
        self.overlay.state.setText("Ошибка · полученный текст сохранён")

    def _finished(self):
        if self.quitting or self.thread is None:
            return
        self.overlay.clear_timeout()
        self._voice_delta("", final=True)
        self.history.add(self.delivery.text)
        self.refresh_history()
        old = self.thread
        self.thread = None
        old.deleteLater()
        if self.settings.load_on_demand:
            self.service.close()
            self.ready = False
        self.clipboard_pending = True
        self._copy_completed_text()
        self.record.setEnabled(self.ready or self.settings.load_on_demand)
        self.record.setText("Начать диктовку · " + hotkey_label(self.settings.hotkey))
        self.meter.setValue(0)
        if not self.last_error:
            self.status.setText(
                "Диктовка завершена · текст введён"
                if self.settings.auto_insert and not self.remainder
                else "Диктовка завершена · текст сохранён в истории"
            )
            self.overlay.state.setText(self.status.text())
        self.tray.setToolTip("talk2g · " + hotkey_label(self.settings.hotkey))
        QTimer.singleShot(1400, self._hide_finished_overlay)

    def _hide_finished_overlay(self):
        if self.thread is None and not self.start_pending:
            self.overlay.hide()

    def _copy_completed_text(self):
        # Never replace a delta while the target is still requesting it from Qt.
        if self.thread is not None or (self.inserter and self.inserter.busy) or not self.clipboard_pending:
            return
        self.clipboard_pending = False
        text = self.delivery.text
        if self.settings.copy_on_stop and text:
            QApplication.clipboard().setText(text)
        else:
            writer = next((item for item in reversed(self.insertion_queues) if item.clipboard_written), None)
            if writer and self.clipboard_before is not None:
                # Leave a newer manual copy alone; restore only our own paste data.
                if is_wayland() or QApplication.clipboard().text() == writer.last_clipboard_text:
                    text = self.clipboard_before.text()
                    QApplication.clipboard().setMimeData(self.clipboard_before)
                else:
                    self.clipboard_before = None
                    return
            else:
                self.clipboard_before = None
                return
        self.clipboard_before = None
        if self.portal:
            try:
                self.portal.set_text(text)
            except Exception:
                log.exception("Не удалось скопировать итог через портал")

    def show_normal(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        if self.quitting or not self.tray.isVisible():
            self.quit()
            event.accept()
        else:
            self.hide()
            event.ignore()

    def quit(self):
        if self.quitting:
            return
        self.quitting = True
        self.start_pending = False
        self.health_timer.stop()
        if self.thread:
            self.thread.cancel()
            if not self.thread.wait(10_000):
                self.quitting = False
                self.health_timer.start()
                self.status.setText("Дождитесь завершения соединения и повторите выход")
                return
            self.history.add(self.delivery.text)
            self.thread.deleteLater()
            self.thread = None
        if self.hotkey:
            self.hotkey.stop()
        if self.portal:
            self.portal.close()
        self.service.close()
        self.history.close()
        self.control.close()
        self.overlay.hide()
        QApplication.quit()


def run_app(settings: Settings, *, background: bool = False):
    application = QApplication(sys.argv[:1])
    application.setApplicationName("talk2g")
    application.setOrganizationName("talk2g")
    application.setQuitOnLastWindowClosed(False)
    if send_control("ping" if background else "show"):
        return
    window = MainWindow(settings)
    if not background or not window.tray.isVisible():
        window.show()
    log.info("Интерфейс готов: pid=%s, фон=%s", os.getpid(), background)
    raise SystemExit(application.exec())
