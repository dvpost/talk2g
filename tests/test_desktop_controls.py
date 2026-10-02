import os
import subprocess
import sys
import time

import pytest
from PySide6.QtCore import QMimeData, QObject, Signal
from PySide6.QtWidgets import QApplication, QPlainTextEdit

from giga_dictation import desktop
from giga_dictation.autostart import Autostart
from giga_dictation.config import Settings


class DictationStub(QObject):
    event = Signal(object)
    failure = Signal(str)
    level = Signal(float)
    connected = Signal()
    finished = Signal()

    def __init__(self, settings, **kwargs):
        super().__init__()
        self.settings = settings
        self.stop_calls = 0

    def start(self):
        self.connected.emit()

    def stop(self):
        self.stop_calls += 1

    def cancel(self):
        self.stop()
        self.finished.emit()

    def wait(self, timeout):
        return True


class ServiceStub:
    def __init__(self, settings):
        self.starts = 0
        self.closes = 0

    def start(self):
        self.starts += 1

    def close(self):
        self.closes += 1


class HotkeyStub:
    def __init__(self, value, callback):
        self.value = value

    def start(self):
        pass

    def stop(self):
        pass


class AutostartStub:
    def __init__(self):
        self.enabled = False

    def is_enabled(self):
        return self.enabled

    def set_enabled(self, enabled):
        self.enabled = enabled


@pytest.fixture
def window(qtbot, monkeypatch, tmp_path):
    monkeypatch.setenv("GIGA_DICTATION_HOME", str(tmp_path))
    monkeypatch.setattr(desktop, "socket_name", lambda: f"giga-controls-{tmp_path.name}")
    monkeypatch.setattr(desktop, "NativeHotkey", HotkeyStub)
    monkeypatch.setattr(desktop, "LocalService", ServiceStub)
    monkeypatch.setattr(desktop, "DictationThread", DictationStub)
    monkeypatch.setattr(desktop, "Autostart", AutostartStub)
    main = desktop.MainWindow(Settings(), start_service=False)
    qtbot.addWidget(main)
    main.health_timer.stop()
    monkeypatch.setattr(main, "check_server", lambda: None)
    yield main
    main.quitting = True
    main.start_pending = False
    if main.thread:
        main.thread.cancel()
    main.overlay.close()
    main.tray.hide()
    main.control.close()
    main.history.close()


def test_tray_switches_are_independent_and_persisted(window, tmp_path):
    window.feature_actions["show_overlay"].trigger()
    assert not window.overlay_option.isChecked()
    assert window.feature_actions["auto_insert"].isChecked()
    assert window.feature_actions["copy_on_stop"].isChecked()
    window.feature_actions["auto_insert"].trigger()
    window.copy_final.setChecked(False)
    saved = Settings.load(tmp_path)
    assert not saved.show_overlay and not saved.auto_insert and not saved.copy_on_stop
    assert saved.hotkey == "<ctrl>+<shift>+a"
    assert not window.feature_actions["copy_on_stop"].isChecked()
    assert window.service.starts == window.service.closes == 0
    window.feature_actions["show_overlay"].trigger()
    assert Settings.load(tmp_path).show_overlay
    assert not Settings.load(tmp_path).auto_insert


def test_autostart_checkbox_installs_and_removes_real_entry_without_restarting_asr(window, tmp_path):
    window.autostart = Autostart(home=tmp_path, config_dir=tmp_path / "xdg", platform="linux")
    window.feature_actions["autostart"].trigger()
    assert window.autostart_option.isChecked() and Settings.load(tmp_path).autostart
    assert window.autostart.is_enabled() and "--background" in window.autostart.entry.read_text()
    window.autostart_option.setChecked(False)
    assert not window.autostart.entry.exists() and not Settings.load(tmp_path).autostart
    assert not window.feature_actions["autostart"].isChecked()
    assert window.service.starts == window.service.closes == 0
    assert window.settings.load_on_demand is False


def test_failed_autostart_install_does_not_leave_checkbox_or_setting_enabled(window, tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("cannot create a directory here")
    window.autostart = Autostart(home=tmp_path, config_dir=blocked, platform="linux")
    window.feature_actions["autostart"].trigger()
    assert not window.autostart_option.isChecked() and not Settings.load(tmp_path).autostart
    assert "Не удалось" in window.status.text()


def test_failed_settings_save_rolls_back_autostart_install(window, monkeypatch, tmp_path):
    window.autostart = Autostart(home=tmp_path, config_dir=tmp_path / "xdg", platform="linux")

    def denied(settings):
        raise PermissionError("settings are read-only")

    monkeypatch.setattr(Settings, "save", denied)
    window.autostart_option.setChecked(True)
    assert not window.autostart.is_enabled() and not window.autostart_option.isChecked()
    assert not window.settings.autostart


def test_voice_stop_checkbox_syncs_and_persists_without_loading_model(window, tmp_path):
    window.feature_actions["stop_on_phrase"].trigger()
    assert window.voice_stop_option.isChecked() and Settings.load(tmp_path).stop_on_phrase
    window.voice_stop_option.setChecked(False)
    assert not window.feature_actions["stop_on_phrase"].isChecked()
    assert not Settings.load(tmp_path).stop_on_phrase
    assert window.service.starts == window.service.closes == 0


def test_dual_window_checkbox_persists_and_defers_active_session_change(window, qtbot, tmp_path):
    window.set_feature("auto_insert", False)
    window.set_feature("load_on_demand", True)
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    assert not window.thread.settings.dual_window
    window.feature_actions["dual_window"].trigger()
    assert window.dual_window_option.isChecked() and Settings.load(tmp_path).dual_window
    assert not window.thread.settings.dual_window and window.thread.stop_calls == 0
    window.thread.finished.emit()
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    assert window.thread.settings.dual_window
    window.dual_window_option.setChecked(False)
    assert not window.feature_actions["dual_window"].isChecked()
    assert not Settings.load(tmp_path).dual_window and window.thread.settings.dual_window


def test_split_voice_command_stops_once_and_cleans_history_and_clipboard(window, qtbot):
    window.set_feature("auto_insert", False)
    window.set_feature("stop_on_phrase", True)
    window.set_feature("load_on_demand", True)
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    thread = window.thread
    raw = ""
    for seq, delta in enumerate(["Текст.", " Конец", " связи.", " Хвост аудио."], 1):
        raw += delta
        window._event({"type": "commit", "seq": seq, "delta": delta})
        assert "конец" not in window.delivery.text.lower()
    assert thread.stop_calls == 1 and not window.record.isEnabled()
    window._event({"type": "session_end", "text": raw})
    assert window.last_error == ""  # filtering must not break protocol integrity validation
    thread.finished.emit()
    assert window.delivery.text == window.confirmed.toPlainText() == "Текст."
    assert QApplication.clipboard().text() == window.history.recent()[0][1] == "Текст."
    assert window.service.closes == 2  # mode change plus normal on-demand finalization
    assert window.thread is None and window.record.isEnabled()
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    window._event({"type": "commit", "seq": 1, "delta": "Новая запись."})
    assert window.delivery.text == "Новая запись." and not window.voice_stop.triggered


def test_partial_command_does_not_stop_until_confirmed(window, qtbot):
    window.set_feature("auto_insert", False)
    window.set_feature("stop_on_phrase", True)
    window.ready = True
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    window._event({"type": "partial", "text": "Конец связи."})
    assert window.thread.stop_calls == 0
    window._event({"type": "commit", "seq": 1, "delta": "Конец недели."})
    assert window.thread.stop_calls == 0 and window.delivery.text == "Конец недели."


def test_voice_mode_can_be_disabled_during_dictation(window, qtbot):
    window.set_feature("auto_insert", False)
    window.set_feature("stop_on_phrase", True)
    window.ready = True
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    window._event({"type": "commit", "seq": 1, "delta": "Текст. Конец"})
    assert window.delivery.text == "Текст."
    window.voice_stop_option.setChecked(False)
    assert window.delivery.text == "Текст. Конец"
    window._event({"type": "commit", "seq": 2, "delta": " связи."})
    assert window.delivery.text == "Текст. Конец связи." and window.thread.stop_calls == 0


def test_hotkey_stop_keeps_held_ordinary_word(window, qtbot):
    window.set_feature("auto_insert", False)
    window.set_feature("stop_on_phrase", True)
    window.ready = True
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    window._event({"type": "commit", "seq": 1, "delta": "Это конец"})
    window.toggle()
    assert window.thread.stop_calls == 1
    window.thread.finished.emit()
    assert window.history.recent()[0][1] == QApplication.clipboard().text() == "Это конец"


def test_first_press_waits_for_model_and_second_press_stops(window, qtbot):
    window.set_feature("auto_insert", False)
    window.toggle()
    assert window.start_pending and window.overlay.isVisible()
    assert window.thread is None
    window._health_result({"ready": True})
    qtbot.waitUntil(lambda: window.thread is not None)
    thread = window.thread
    window.toggle()
    assert thread.stop_calls == 1
    thread.finished.emit()
    assert window.thread is None


def test_second_press_cancels_waiting_start(window, qtbot):
    window.set_feature("auto_insert", False)
    window.toggle()
    window.toggle()
    window._health_result({"ready": True})
    qtbot.wait(300)
    assert window.thread is None and not window.start_pending
    assert not window.overlay.isVisible()


def test_on_demand_checkbox_persists_and_unloads_then_warms_when_unchecked(window, tmp_path):
    window.feature_actions["load_on_demand"].trigger()
    assert window.demand_option.isChecked() and Settings.load(tmp_path).load_on_demand
    assert window.record.isEnabled() and not window.ready
    assert window.service.closes == 1 and window.service.starts == 0
    window._health_result({"ready": True})  # stale polling response cannot revive an unloaded model
    assert not window.ready and window.record.isEnabled()
    window.demand_option.setChecked(False)
    assert not Settings.load(tmp_path).load_on_demand
    assert not window.feature_actions["load_on_demand"].isChecked()
    assert window.service.starts == 1 and not window.record.isEnabled()


def test_app_starts_without_loading_model_in_on_demand_mode(qtbot, monkeypatch, tmp_path):
    monkeypatch.setenv("GIGA_DICTATION_HOME", str(tmp_path))
    monkeypatch.setattr(desktop, "socket_name", lambda: f"giga-cold-start-{tmp_path.name}")
    monkeypatch.setattr(desktop, "NativeHotkey", HotkeyStub)
    monkeypatch.setattr(desktop, "LocalService", ServiceStub)
    monkeypatch.setattr(desktop, "Autostart", AutostartStub)
    main = desktop.MainWindow(Settings(load_on_demand=True))
    qtbot.addWidget(main)
    try:
        main.health_timer.stop()
        main.check_server()
        assert main.service.starts == 0 and not main.checking
        assert main.record.isEnabled() and main.demand_option.isChecked() and not main.ready
    finally:
        main.quitting = True
        main.hotkey.stop()
        main.overlay.close()
        main.tray.hide()
        main.control.close()
        main.history.close()


def test_cold_hotkey_records_before_readiness_and_unloads_between_sessions(window, qtbot):
    window.set_feature("auto_insert", False)
    window.set_feature("load_on_demand", True)
    for _ in range(2):
        before_starts, before_closes = window.service.starts, window.service.closes
        window.toggle()
        qtbot.waitUntil(lambda: window.thread is not None)
        thread = window.thread
        assert not window.ready and window.service.starts == before_starts + 1
        window.toggle()  # Stop before the model has loaded still closes recording on the first press.
        assert thread.stop_calls == 1
        thread.finished.emit()
        assert window.service.closes == before_closes + 1
        assert window.thread is None and window.record.isEnabled() and not window.ready


def test_switching_model_lifetime_during_recording_defers_until_finish(window, qtbot):
    window.set_feature("auto_insert", False)
    window.ready = True
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    window.set_feature("load_on_demand", True)
    assert window.service.closes == 0 and window.thread.stop_calls == 0
    window.thread.finished.emit()
    assert window.service.closes == 1 and window.record.isEnabled()


def test_overlay_can_be_switched_during_dictation(window, qtbot):
    window.set_feature("auto_insert", False)
    window.ready = True
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    assert window.overlay.isVisible()
    window.feature_actions["show_overlay"].trigger()
    assert not window.overlay.isVisible()
    window.feature_actions["show_overlay"].trigger()
    assert window.overlay.isVisible()
    assert window.thread.stop_calls == 0
    window._hide_finished_overlay()
    assert window.overlay.isVisible()  # a previous session's timer cannot hide this one


def test_disabling_both_outputs_preserves_clipboard_and_keeps_history(window, qtbot):
    window.set_feature("auto_insert", False)
    window.set_feature("copy_on_stop", False)
    window.set_feature("show_overlay", False)
    QApplication.clipboard().setText("Прежний буфер")
    window.ready = True
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    window._event({"type": "commit", "seq": 1, "delta": "Новая диктовка."})
    window.thread.finished.emit()
    assert QApplication.clipboard().text() == "Прежний буфер"
    assert window.history.recent()[0][1] == "Новая диктовка."
    assert not window.overlay.isVisible()


@pytest.mark.desktop
@pytest.mark.skipif(
    not os.environ.get("GIGA_DESKTOP_TESTS") or sys.platform != "linux", reason="Native X11 insertion"
)
def test_copy_disabled_restores_rich_clipboard_after_real_insertion(window, qtbot):
    target = QPlainTextEdit()
    qtbot.addWidget(target)
    target.show()
    target.setFocus()
    subprocess.run(["xdotool", "windowactivate", "--sync", str(int(target.winId()))], check=True)
    qtbot.waitUntil(lambda: target.isActiveWindow() and target.hasFocus())
    original = QMimeData()
    original.setText("Старый буфер")
    original.setHtml("<b>Старый буфер</b>")
    QApplication.clipboard().setMimeData(original)
    window.set_feature("copy_on_stop", False)
    window.set_feature("show_overlay", False)
    window.ready = True
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    window._event({"type": "commit", "seq": 1, "delta": "Введённый текст."})
    window.thread.finished.emit()  # the final clipboard update must await the paste queue
    qtbot.waitUntil(lambda: not window.inserter.busy, timeout=5000)
    qtbot.waitUntil(lambda: target.toPlainText() == "Введённый текст.", timeout=2000)
    assert QApplication.clipboard().text() == "Старый буфер"
    assert QApplication.clipboard().mimeData().html() == "<b>Старый буфер</b>"
    assert not window.overlay.isVisible()


@pytest.mark.desktop
@pytest.mark.skipif(
    not os.environ.get("GIGA_DESKTOP_TESTS") or sys.platform != "linux", reason="Native X11 clipboard"
)
def test_external_clipboard_does_not_delay_opening_the_microphone(window, qtbot):
    subprocess.run(["xclip", "-selection", "clipboard", "-in"], input="Чужой буфер", text=True, check=True)
    qtbot.wait(100)
    window.set_feature("auto_insert", False)
    window.ready = True
    began = time.monotonic()
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None, timeout=1500)
    assert time.monotonic() - began < 1
    assert window.clipboard_before.text() == "Чужой буфер"


@pytest.mark.desktop
@pytest.mark.skipif(
    not os.environ.get("GIGA_DESKTOP_TESTS") or sys.platform != "linux", reason="Native X11 insertion"
)
def test_auto_insert_can_be_disabled_and_reenabled_mid_session(window, qtbot):
    target = QPlainTextEdit()
    qtbot.addWidget(target)
    target.show()
    target.setFocus()
    subprocess.run(["xdotool", "windowactivate", "--sync", str(int(target.winId()))], check=True)
    qtbot.waitUntil(lambda: target.isActiveWindow() and target.hasFocus())
    window.ready = True
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    window._event({"type": "commit", "seq": 1, "delta": "Отключённая часть. "})
    window.feature_actions["auto_insert"].trigger()  # cancel a queued, not-yet-pasted delta
    qtbot.wait(300)
    assert target.toPlainText() == ""
    window._event({"type": "commit", "seq": 2, "delta": "Ещё отключённая часть. "})
    window.feature_actions["auto_insert"].trigger()
    window._event({"type": "commit", "seq": 3, "delta": "Включённая часть."})
    qtbot.waitUntil(lambda: target.toPlainText() == "Включённая часть.", timeout=5000)
    window.thread.finished.emit()
    qtbot.waitUntil(lambda: not window.inserter.busy, timeout=5000)
    assert QApplication.clipboard().text() == ("Отключённая часть. Ещё отключённая часть. Включённая часть.")


@pytest.mark.desktop
@pytest.mark.skipif(
    not os.environ.get("GIGA_DESKTOP_TESTS") or sys.platform != "linux", reason="Native X11 insertion"
)
def test_voice_command_never_leaks_into_real_target_or_clipboard(window, qtbot):
    target = QPlainTextEdit()
    qtbot.addWidget(target)
    target.show()
    target.setFocus()
    subprocess.run(["xdotool", "windowactivate", "--sync", str(int(target.winId()))], check=True)
    qtbot.waitUntil(lambda: target.isActiveWindow() and target.hasFocus())
    window.set_feature("stop_on_phrase", True)
    window.ready = True
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    thread = window.thread
    window._event({"type": "commit", "seq": 1, "delta": "Проверка. Конец"})
    qtbot.waitUntil(lambda: target.toPlainText() == "Проверка.", timeout=5000)
    window._event({"type": "commit", "seq": 2, "delta": " связи."})
    assert thread.stop_calls == 1
    window._event({"type": "session_end", "text": "Проверка. Конец связи."})
    thread.finished.emit()
    qtbot.waitUntil(lambda: not window.inserter.busy, timeout=5000)
    assert target.toPlainText() == QApplication.clipboard().text() == "Проверка."
