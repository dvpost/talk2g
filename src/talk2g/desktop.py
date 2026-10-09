from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from dataclasses import replace

from PySide6.QtCore import QMimeData, QObject, QSignalBlocker, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .autostart import Autostart
from .client import DictationThread
from .config import Settings
from .dictated_text import DictatedText
from .history import History
from .hotkey import NativeHotkey, parse_hotkey
from .input import foreground, is_wayland
from .insertion import InsertionQueue
from .recording import recordings_directory
from .service import LocalService, health
from .ui.dictation import DictationPage
from .ui.history import HistoryPage
from .ui.overlay import Overlay
from .ui.settings import SettingsPage

log = logging.getLogger(__name__)


def socket_name() -> str:
    return "talk2g-" + (str(os.getuid()) if hasattr(os, "getuid") else os.environ["USERNAME"])


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


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings, *, start_service: bool = True):
        super().__init__()
        self.autostart = Autostart()
        settings = replace(settings, autostart=self.autostart.is_enabled())
        self.settings = settings
        self.service = LocalService(settings)
        self.history = History()
        self.thread: DictationThread | None = None
        self.dictated_text = DictatedText()
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
        self.dictation = DictationPage(hotkey_label(self.settings.hotkey))
        self.dictation.toggle_requested.connect(self.toggle)
        self.dictation.copy_requested.connect(
            lambda: QApplication.clipboard().setText(self.dictated_text.text)
        )
        self.tabs.addTab(self.dictation, "Диктовка")
        self.preferences = SettingsPage(self.settings)
        self.preferences.feature_changed.connect(self.set_feature)
        self.preferences.position_changed.connect(self.set_overlay_position)
        self.preferences.save_requested.connect(self.save_settings)
        self.preferences.wayland_requested.connect(self.allow_wayland)
        self.preferences.open_recordings_requested.connect(self.open_recordings)
        if self.preferences.microphone_error:
            self.status.setText(self.preferences.microphone_error)
        self.tabs.addTab(self.preferences, "Настройки")
        self.history_page = HistoryPage(self.history)
        self.tabs.addTab(self.history_page, "История")
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

    def open_recordings(self):
        directory = recordings_directory()
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory))):
                raise OSError("Не удалось открыть файловый менеджер")
        except OSError as error:
            self.status.setText(f"Папка записей: {error}")

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
            elif command == "show":
                self.show_normal()
            else:
                log.warning("Неизвестная команда локального управления")
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
        self.dictation.record.setEnabled(self.ready)
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
            new = self.preferences.values(self.settings)
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
            self.dictation.record.setText("Начать диктовку · " + hotkey_label(new.hotkey))
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
        self.preferences.sync(self.settings)
        for name, action in self.feature_actions.items():
            with QSignalBlocker(action):
                action.setChecked(getattr(self.settings, name))
        self.overlay.set_position(self.settings.overlay_position)

    def set_overlay_position(self):
        position = self.preferences.overlay_position_field.currentData()
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
                    if self.dictation.record.isEnabled():
                        self.overlay.set_idle_enabled(enabled)
            elif name == "stop_on_phrase" and self.thread:
                self._flush_text()  # disabling releases a held ordinary word immediately
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
                    self.dictation.record.setEnabled(False)
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
        self.dictation.record.setEnabled(True)
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
        if self.thread and self.dictation.record.isEnabled():
            self.thread.stop()
            self.overlay.clear_timeout()
            self.status.setText("Завершаю оставшийся хвост…")
            self.overlay.state.setText("Завершаю оставшийся хвост…")
            self.dictation.record.setEnabled(False)

    def _idle_remaining(self, remaining):
        if (
            self.thread
            and self.sender() is self.thread
            and self.settings.stop_on_idle
            and not self.quitting
            and self.dictation.record.isEnabled()
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
            and not self.quitting
            and self.dictation.record.isEnabled()
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
        self.dictated_text = DictatedText()
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
        self.dictation.confirmed.clear()
        self._create_inserter(target)
        self.dictation.startup_info.clear()
        self.dictation.recording_info.clear()
        self.thread = DictationThread(
            self.settings, audio_file=audio_file, activated_at=self.activated_at or time.monotonic()
        )
        self.thread.connected.connect(lambda: self.status.setText("Слушаю…"))
        self.thread.event.connect(self._event)
        self.thread.failure.connect(self._failure)
        self.thread.level.connect(lambda value: self.dictation.meter.setValue(min(100, int(value * 500))))
        self.thread.idle_remaining.connect(self._idle_remaining)
        self.thread.idle_expired.connect(self._idle_expired)
        self.thread.pause_remaining.connect(self._pause_remaining)
        self.thread.finished.connect(self._finished)
        self.status.setText("Подключаю микрофон…")
        self.dictation.record.setText("Остановить диктовку")
        self.dictation.record.setEnabled(True)
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
                self.dictation.recording_info.setText("Не удалось сохранить аудио: " + event["message"])
                log.warning("Сохранение аудио: %s", event["message"])
            else:
                label = "Аудиозапись сохранена" if event["status"] == "saved" else "Сохранение аудио"
                self.dictation.recording_info.setText(f"{label}: {event['path']}")
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
            self.dictation.startup_info.setText(
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
                clean, stop = self.dictated_text.accept(event, stop_on_phrase=self.settings.stop_on_phrase)
            except Exception as error:
                self._failure(str(error))
                self.thread.cancel()
                return
            self._apply_text(clean, stop)
        elif event["type"] == "recognizing":
            message = (
                "Распознаю блок · запись продолжается"
                if self.dictation.record.isEnabled()
                else "Распознаю оставшийся блок…"
            )
            self.status.setText(message)
            self.overlay.state.setText(message)
        elif event["type"] == "segment_end":
            self.overlay.preview.setText(self.dictated_text.text[-200:])
            if self.thread and self.dictation.record.isEnabled():
                self.status.setText("Слушаю…")
                self.overlay.state.setText("Слушаю · горячая клавиша — завершить")
        elif event["type"] == "session_end":
            self._flush_text(final=True)
            if not self.dictated_text.reconcile(event["text"]):
                self._failure(
                    "Итог сервера отличается от полученных фрагментов. "
                    "Подтверждённый текст сохранён в истории"
                )
                self.dictation.confirmed.setPlainText(self.dictated_text.text)

    def _flush_text(self, *, final=False):
        clean, stop = self.dictated_text.flush(stop_on_phrase=self.settings.stop_on_phrase, final=final)
        self._apply_text(clean, stop)

    def _apply_text(self, clean: str, stop: bool):
        self.dictation.confirmed.setPlainText(self.dictated_text.text)
        bar = self.dictation.confirmed.verticalScrollBar()
        bar.setValue(bar.maximum())
        if clean:
            if self.settings.auto_insert:
                self.inserter.add(clean)
            else:
                self.remainder += clean
        self.overlay.preview.setText(self.dictated_text.text[-200:])
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
        self._flush_text(final=True)
        self.history.add(self.dictated_text.text)
        self.history_page.refresh()
        old = self.thread
        self.thread = None
        old.deleteLater()
        if self.settings.load_on_demand:
            self.service.close()
            self.ready = False
        self.clipboard_pending = True
        self._copy_completed_text()
        self.dictation.record.setEnabled(self.ready or self.settings.load_on_demand)
        self.dictation.record.setText("Начать диктовку · " + hotkey_label(self.settings.hotkey))
        self.dictation.meter.setValue(0)
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
        try:
            self._update_completed_clipboard()
        except Exception as error:
            self._failure(f"Не удалось обновить буфер обмена: {error}")
        finally:
            self.clipboard_before = None

    def _update_completed_clipboard(self):
        text = self.dictated_text.text
        restore = not (self.settings.copy_on_stop and text)
        if restore:
            writer = next((item for item in reversed(self.insertion_queues) if item.clipboard_written), None)
            if writer is None or self.clipboard_before is None:
                return
            # Leave a newer manual copy alone; restore only our own paste data.
            if not is_wayland() and QApplication.clipboard().text() != writer.last_clipboard_text:
                return
            text = self.clipboard_before.text()
        if is_wayland():
            if self.portal is None:
                raise RuntimeError("Сначала разрешите доступ к буферу обмена через портал Wayland")
            self.portal.set_text(text)
        elif restore:
            QApplication.clipboard().setMimeData(self.clipboard_before)
        else:
            QApplication.clipboard().setText(text)

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
            self.history.add(self.dictated_text.text)
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
