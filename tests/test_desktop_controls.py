import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QMimeData, QObject, Signal
from PySide6.QtWidgets import QApplication, QPlainTextEdit

from talk2g import desktop
from talk2g.autostart import Autostart
from talk2g.config import Settings


class DictationStub(QObject):
    event = Signal(object)
    failure = Signal(str)
    level = Signal(float)
    connected = Signal()
    idle_remaining = Signal(float)
    idle_expired = Signal()
    pause_remaining = Signal(float)
    finished = Signal()

    def __init__(self, settings, **kwargs):
        super().__init__()
        self.settings = settings
        self.stop_calls = 0
        self.idle_changes = []

    def start(self):
        self.connected.emit()
        if self.settings.stop_on_idle:
            self.idle_remaining.emit(self.settings.idle_timeout)

    def set_idle_enabled(self, enabled):
        self.idle_changes.append(enabled)
        if enabled:
            self.idle_remaining.emit(self.settings.idle_timeout)

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
    monkeypatch.setenv("TALK2G_HOME", str(tmp_path))
    monkeypatch.setattr(desktop, "socket_name", lambda: f"talk2g-controls-{tmp_path.name}")
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
    # GIVEN: настройки по умолчанию и независимые переключатели.
    # WHEN: меняем переключатели в трее и форме.
    window.feature_actions["show_overlay"].trigger()
    # THEN: форма, трей и сохранённые настройки согласованы.
    assert not window.preferences.overlay_option.isChecked()
    assert window.feature_actions["auto_insert"].isChecked()
    assert window.feature_actions["copy_on_stop"].isChecked()
    window.feature_actions["auto_insert"].trigger()
    window.preferences.copy_final.setChecked(False)
    saved = Settings.load(tmp_path)
    assert not saved.show_overlay and not saved.auto_insert and not saved.copy_on_stop
    assert saved.hotkey == "<ctrl>+<shift>+a"
    assert not window.feature_actions["copy_on_stop"].isChecked()
    assert window.service.starts == window.service.closes == 0
    window.feature_actions["show_overlay"].trigger()
    assert Settings.load(tmp_path).show_overlay
    assert not Settings.load(tmp_path).auto_insert


def test_autostart_checkbox_installs_and_removes_real_entry_without_restarting_asr(window, tmp_path):
    # GIVEN: реальный Linux-автозапуск в временной папке.
    window.autostart = Autostart(home=tmp_path, config_dir=tmp_path / "xdg", platform="linux")
    # WHEN: включаем и выключаем автозапуск.
    window.feature_actions["autostart"].trigger()
    # THEN: системная запись и настройки меняются без перезапуска модели.
    assert window.preferences.autostart_option.isChecked() and Settings.load(tmp_path).autostart
    assert window.autostart.is_enabled() and "--background" in window.autostart.entry.read_text()
    window.preferences.autostart_option.setChecked(False)
    assert not window.autostart.entry.exists() and not Settings.load(tmp_path).autostart
    assert not window.feature_actions["autostart"].isChecked()
    assert window.service.starts == window.service.closes == 0
    assert window.settings.load_on_demand is False


def test_failed_autostart_install_does_not_leave_checkbox_or_setting_enabled(window, tmp_path):
    # GIVEN: путь автозапуска заблокирован файлом.
    blocked = tmp_path / "blocked"
    blocked.write_text("cannot create a directory here")
    window.autostart = Autostart(home=tmp_path, config_dir=blocked, platform="linux")
    # WHEN: пытаемся включить автозапуск.
    window.feature_actions["autostart"].trigger()
    # THEN: переключатель откатывается и показывается ошибка.
    assert not window.preferences.autostart_option.isChecked() and not Settings.load(tmp_path).autostart
    assert "Не удалось" in window.status.text()


def test_failed_settings_save_rolls_back_autostart_install(window, monkeypatch, tmp_path):
    # GIVEN: сохранение настроек запрещено.
    window.autostart = Autostart(home=tmp_path, config_dir=tmp_path / "xdg", platform="linux")

    def denied(settings):
        raise PermissionError("settings are read-only")

    monkeypatch.setattr(Settings, "save", denied)
    # WHEN: включаем автозапуск.
    window.preferences.autostart_option.setChecked(True)
    # THEN: системная запись и переключатель откатываются.
    assert not window.autostart.is_enabled() and not window.preferences.autostart_option.isChecked()
    assert not window.settings.autostart


def test_unreadable_autostart_does_not_use_saved_flag(window, monkeypatch):
    # GIVEN: системное состояние автозапуска недоступно, профиль содержит прежнее значение.
    def denied(self):
        raise PermissionError("autostart is unreadable")

    monkeypatch.setattr(AutostartStub, "is_enabled", denied)
    # WHEN: приложение создаёт новый контроллер.
    with pytest.raises(PermissionError, match="autostart is unreadable"):
        desktop.MainWindow(Settings(autostart=True), start_service=False)
    # THEN: исходный экземпляр приложения не изменяется.
    assert not window.settings.autostart


def test_mismatched_terminal_result_preserves_confirmed_history_and_clipboard(window):
    # GIVEN: два подтверждённых фрагмента без автоматического системного ввода.
    window.set_feature("auto_insert", False)
    window.start_recording()
    thread = window.thread
    thread.event.emit({"type": "commit", "seq": 1, "delta": "Первый."})
    thread.event.emit({"type": "commit", "seq": 2, "delta": " Второй."})
    # WHEN: сервер возвращает несовпадающий итог, затем поток заканчивается.
    thread.event.emit({"type": "session_end", "text": "Другой результат."})
    thread.finished.emit()
    # THEN: видна ошибка, подтверждённый текст не подменён в UI, истории и буфере.
    assert "Итог сервера отличается" in window.last_error
    assert window.dictation.confirmed.toPlainText() == "Первый. Второй."
    assert window.history.recent()[0][1] == QApplication.clipboard().text() == "Первый. Второй."


def test_client_protocol_failure_preserves_confirmed_text_history_and_clipboard(window):
    # GIVEN: диктовка уже получила подтверждённый текст.
    window.set_feature("auto_insert", False)
    window.start_recording()
    thread = window.thread
    thread.event.emit({"type": "commit", "seq": 1, "delta": "Подтверждено."})
    # WHEN: сетевой клиент отклоняет следующий ответ и завершает поток.
    thread.failure.emit("Некорректный ответ сервера: поле recording.message")
    thread.finished.emit()
    # THEN: ошибка видна, подтверждённый текст сохранён; история содержит ровно одну запись.
    assert window.last_error == "Ошибка: Некорректный ответ сервера: поле recording.message"
    assert window.dictation.confirmed.toPlainText() == QApplication.clipboard().text() == "Подтверждено."
    assert len(window.history.recent()) == 1 and window.history.recent()[0][1] == "Подтверждено."


@pytest.fixture
def portal_copy(window, monkeypatch):
    window.set_feature("auto_insert", False)
    QApplication.clipboard().setText("Исходный буфер")
    window.start_recording()
    thread = window.thread
    thread.event.emit({"type": "commit", "seq": 1, "delta": "Диктовка."})
    monkeypatch.setattr(desktop, "is_wayland", lambda: True)

    def prepare(*, denied):
        calls = []

        def copy(text):
            calls.append(text)
            if denied:
                raise RuntimeError("portal denied")

        window.portal = SimpleNamespace(set_text=copy, close=lambda: None)
        return thread, calls

    return prepare


def test_wayland_final_copy_uses_portal_once_without_qt_fallback(window, portal_copy):
    # GIVEN: Wayland разрешает финальное копирование через портал.
    thread, calls = portal_copy(denied=False)
    # WHEN: заканчивается сессия.
    thread.finished.emit()
    # THEN: один вызов портала, Qt-буфер не используется как второй способ; отказ виден.
    assert calls == ["Диктовка."]
    assert QApplication.clipboard().text() == "Исходный буфер"
    assert not window.last_error
    assert window.history.recent()[0][1] == "Диктовка."


def test_wayland_copy_denial_is_visible_without_qt_fallback(window, portal_copy):
    # GIVEN: Wayland-портал отказывает в копировании, исходный Qt-буфер сохранён.
    thread, calls = portal_copy(denied=True)
    # WHEN: заканчивается диктовка.
    thread.finished.emit()
    # THEN: отказ виден; нет второй попытки, Qt-буфер сохранён, текст остаётся в истории.
    assert calls == ["Диктовка."]
    assert QApplication.clipboard().text() == "Исходный буфер"
    assert "portal denied" in window.last_error
    assert window.history.recent()[0][1] == "Диктовка."


def test_voice_stop_checkbox_syncs_and_persists_without_loading_model(window, tmp_path):
    # GIVEN: выключенная голосовая команда.
    # WHEN: меняем переключатель команды в трее и форме.
    window.feature_actions["stop_on_phrase"].trigger()
    # THEN: настройка сохраняется без загрузки модели.
    assert window.preferences.voice_stop_option.isChecked() and Settings.load(tmp_path).stop_on_phrase
    window.preferences.voice_stop_option.setChecked(False)
    assert not window.feature_actions["stop_on_phrase"].isChecked()
    assert not Settings.load(tmp_path).stop_on_phrase
    assert window.service.starts == window.service.closes == 0


def test_split_voice_command_stops_once_and_cleans_history_and_clipboard(window, qtbot):
    # GIVEN: диктовка с голосовой командой и загрузкой модели по требованию.
    # WHEN: получаем команду частями и завершаем сессию.
    window.set_feature("auto_insert", False)
    window.set_feature("stop_on_phrase", True)
    window.set_feature("load_on_demand", True)
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    thread = window.thread
    raw = ""
    for seq, delta in enumerate(["Текст.", " Конец", " связи.", " Хвост аудио."], 1):
        raw += delta
        window.thread.event.emit({"type": "commit", "seq": seq, "delta": delta})
        # THEN: остановка однократна, команда и хвост исключены из результата.
        assert "конец" not in window.dictated_text.text.lower()
    assert thread.stop_calls == 1 and not window.dictation.record.isEnabled()
    window.thread.event.emit({"type": "session_end", "text": raw})
    assert window.last_error == ""  # filtering must not break protocol integrity validation
    thread.finished.emit()
    assert window.dictated_text.text == window.dictation.confirmed.toPlainText() == "Текст."
    assert QApplication.clipboard().text() == window.history.recent()[0][1] == "Текст."
    assert window.service.closes == 2  # mode change plus normal on-demand finalization
    assert window.thread is None and window.dictation.record.isEnabled()
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    window.thread.event.emit({"type": "commit", "seq": 1, "delta": "Новая запись."})
    assert window.dictated_text.text == "Новая запись." and window.thread.stop_calls == 0


def test_voice_mode_can_be_disabled_during_dictation(window, qtbot):
    # GIVEN: диктовка с включённой голосовой командой.
    # WHEN: отключаем команду между двумя фрагментами.
    window.set_feature("auto_insert", False)
    window.set_feature("stop_on_phrase", True)
    window.ready = True
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    window.thread.event.emit({"type": "commit", "seq": 1, "delta": "Текст. Конец"})
    # THEN: удержанное обычное слово сохраняется и диктовка продолжается.
    assert window.dictated_text.text == "Текст."
    window.preferences.voice_stop_option.setChecked(False)
    assert window.dictated_text.text == "Текст. Конец"
    window.thread.event.emit({"type": "commit", "seq": 2, "delta": " связи."})
    assert window.dictated_text.text == "Текст. Конец связи." and window.thread.stop_calls == 0


def test_hotkey_stop_keeps_held_ordinary_word(window, qtbot):
    # GIVEN: голосовая команда включена, последнее обычное слово удержано.
    window.set_feature("auto_insert", False)
    window.set_feature("stop_on_phrase", True)
    window.ready = True
    # WHEN: пользователь завершает запись горячей клавишей.
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    window.thread.event.emit({"type": "commit", "seq": 1, "delta": "Это конец"})
    window.toggle()
    # THEN: обычное слово сохраняется в истории и буфере.
    assert window.thread.stop_calls == 1
    window.thread.finished.emit()
    assert window.history.recent()[0][1] == QApplication.clipboard().text() == "Это конец"


def test_first_press_waits_for_model_and_second_press_stops(window, qtbot):
    # GIVEN: модель ещё не готова.
    window.set_feature("auto_insert", False)
    # WHEN: нажимаем хоткей, получаем готовность и нажимаем хоткей повторно.
    window.toggle()
    # THEN: диктовка запускается после готовности и останавливается однократно.
    assert window.start_pending and window.overlay.isVisible()
    assert window.thread is None
    window.bridge.health_result.emit({"ready": True})
    qtbot.waitUntil(lambda: window.thread is not None)
    thread = window.thread
    window.toggle()
    assert thread.stop_calls == 1
    thread.finished.emit()
    assert window.thread is None


def test_second_press_cancels_waiting_start(window, qtbot):
    # GIVEN: модель ещё не готова.
    window.set_feature("auto_insert", False)
    # WHEN: дважды нажимаем хоткей до сообщения готовности.
    window.toggle()
    window.toggle()
    window.bridge.health_result.emit({"ready": True})
    qtbot.wait(300)
    # THEN: отложенный запуск отменяется.
    assert window.thread is None and not window.start_pending
    assert not window.overlay.isVisible()


def test_on_demand_checkbox_persists_and_unloads_then_warms_when_unchecked(window, tmp_path):
    # GIVEN: постоянно загруженная модель.
    # WHEN: меняем режим загрузки через трей и форму.
    window.feature_actions["load_on_demand"].trigger()
    # THEN: жизненный цикл модели соответствует сохранённой настройке.
    assert window.preferences.demand_option.isChecked() and Settings.load(tmp_path).load_on_demand
    assert window.dictation.record.isEnabled() and not window.ready
    assert window.service.closes == 1 and window.service.starts == 0
    window.bridge.health_result.emit(
        {"ready": True}
    )  # stale polling response cannot revive an unloaded model
    assert not window.ready and window.dictation.record.isEnabled()
    window.preferences.demand_option.setChecked(False)
    assert not Settings.load(tmp_path).load_on_demand
    assert not window.feature_actions["load_on_demand"].isChecked()
    assert window.service.starts == 1 and not window.dictation.record.isEnabled()


def test_app_starts_without_loading_model_in_on_demand_mode(qtbot, monkeypatch, tmp_path):
    # GIVEN: режим загрузки модели по требованию и подменённые системные службы.
    monkeypatch.setenv("TALK2G_HOME", str(tmp_path))
    monkeypatch.setattr(desktop, "socket_name", lambda: f"talk2g-cold-start-{tmp_path.name}")
    monkeypatch.setattr(desktop, "NativeHotkey", HotkeyStub)
    monkeypatch.setattr(desktop, "LocalService", ServiceStub)
    monkeypatch.setattr(desktop, "Autostart", AutostartStub)
    # WHEN: создаём окно приложения.
    main = desktop.MainWindow(Settings(load_on_demand=True))
    qtbot.addWidget(main)
    try:
        main.health_timer.stop()
        main.check_server()
        # THEN: модель не загружается, кнопка диктовки доступна.
        assert main.service.starts == 0 and not main.checking
        assert (
            main.dictation.record.isEnabled()
            and main.preferences.demand_option.isChecked()
            and not main.ready
        )
    finally:
        main.quitting = True
        main.hotkey.stop()
        main.overlay.close()
        main.tray.hide()
        main.control.close()
        main.history.close()


def test_cold_hotkey_records_before_readiness_and_unloads_between_sessions(window, qtbot):
    # GIVEN: режим загрузки по требованию.
    # WHEN: дважды запускаем и останавливаем диктовку до готовности модели.
    window.set_feature("auto_insert", False)
    window.set_feature("load_on_demand", True)
    for _ in range(2):
        before_starts, before_closes = window.service.starts, window.service.closes
        window.toggle()
        qtbot.waitUntil(lambda: window.thread is not None)
        thread = window.thread
        # THEN: захват начинается сразу, собственный сервер закрывается после каждой сессии.
        assert not window.ready and window.service.starts == before_starts + 1
        window.toggle()  # Stop before the model has loaded still closes recording on the first press.
        assert thread.stop_calls == 1
        thread.finished.emit()
        assert window.service.closes == before_closes + 1
        assert window.thread is None and window.dictation.record.isEnabled() and not window.ready


def test_switching_model_lifetime_during_recording_defers_until_finish(window, qtbot):
    # GIVEN: активная диктовка.
    # WHEN: включаем загрузку по требованию.
    window.set_feature("auto_insert", False)
    window.ready = True
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    window.set_feature("load_on_demand", True)
    # THEN: модель выгружается только после завершения.
    assert window.service.closes == 0 and window.thread.stop_calls == 0
    window.thread.finished.emit()
    assert window.service.closes == 1 and window.dictation.record.isEnabled()


def test_overlay_can_be_switched_during_dictation(window, qtbot, monkeypatch):
    # GIVEN: активная диктовка.
    window.set_feature("auto_insert", False)
    window.ready = True
    timers = []
    monkeypatch.setattr(
        desktop.QTimer, "singleShot", lambda delay, callback: timers.append((delay, callback))
    )
    window.start_recording()
    window.thread.finished.emit()
    # WHEN: выключаем и включаем плавающее окно.
    window.start_recording()
    # THEN: видимость меняется без остановки записи.
    assert window.overlay.isVisible()
    window.feature_actions["show_overlay"].trigger()
    assert not window.overlay.isVisible()
    window.feature_actions["show_overlay"].trigger()
    assert window.overlay.isVisible()
    assert window.thread.stop_calls == 0
    assert len(timers) == 1 and timers[0][0] == 1400
    timers[0][1]()  # Срабатывает зарегистрированный Qt-таймер предыдущей сессии.
    assert window.overlay.isVisible()


def test_idle_timeout_setting_is_saved_without_restarting_model(window, tmp_path):
    # GIVEN: тайм-аут по умолчанию 45 секунд.
    assert window.preferences.idle_timeout_field.value() == 45
    # WHEN: задаём 90 секунд и сохраняем форму.
    window.preferences.idle_timeout_field.setValue(90)
    window.save_settings()
    # THEN: новое значение сохраняется без перезапуска модели.
    assert window.settings.idle_timeout == Settings.load(tmp_path).idle_timeout == 90
    assert window.service.starts == window.service.closes == 0


def test_recognition_pause_is_saved_without_a_mode_switch(window, tmp_path):
    # GIVEN: распознавание после паузы 3 секунды.
    assert window.preferences.recognition_pause_field.value() == 3
    assert window.preferences.recognition_pause_field.isEnabled()
    # WHEN: задаём паузу 5 секунд.
    window.preferences.recognition_pause_field.setValue(5)
    window.save_settings()
    # THEN: длительность сохраняется; переключателя алгоритма в форме и трее нет.
    assert Settings.load(tmp_path).recognition_pause == 5
    assert "recognize_on_pause" not in window.feature_actions
    assert not hasattr(window.preferences, "pause_option")
    assert window.service.starts == window.service.closes == 0


def test_single_bar_has_yellow_recognition_and_blue_session_parts_without_caption(window, qtbot):
    # GIVEN: диктовка с двумя таймерами.
    window.set_feature("auto_insert", False)
    window.ready = True
    # WHEN: приходят обновления паузы и автозавершения.
    window.start_recording()
    thread, bar = window.thread, window.overlay.timeout_bar
    thread.pause_remaining.emit(3)
    # THEN: единая полоса показывает оба отсчёта и скрывается после Stop.
    assert bar.fractions() == pytest.approx((42 / 45, 3 / 45))
    thread.idle_remaining.emit(43.5)
    thread.pause_remaining.emit(1.5)
    assert bar.fractions() == pytest.approx((42 / 45, 1.5 / 45))
    thread.idle_remaining.emit(42)
    thread.pause_remaining.emit(0)
    assert bar.fractions() == pytest.approx((42 / 45, 0))
    thread.idle_remaining.emit(45)
    thread.pause_remaining.emit(3)
    assert bar.fractions() == pytest.approx((42 / 45, 3 / 45))
    assert not hasattr(window.overlay, "countdown")
    window.stop_recording()
    thread.pause_remaining.emit(3)
    assert bar.isHidden()


def test_pause_bar_survives_disabling_idle_stop_and_new_sessions(window, qtbot):
    # GIVEN: активная диктовка с распознаванием после паузы.
    window.set_feature("auto_insert", False)
    window.ready = True
    # WHEN: отключаем автозавершение.
    window.start_recording()
    thread = window.thread
    thread.pause_remaining.emit(1.5)
    window.set_feature("stop_on_idle", False)
    bar = window.overlay.timeout_bar
    # THEN: жёлтая полоса остаётся, распознавание по паузам работает и в следующей сессии.
    assert bar.isVisible() and bar.fractions() == (0, 0.5)
    assert thread.stop_calls == 0
    thread.pause_remaining.emit(3)
    assert bar.fractions() == (0, 1)
    thread.finished.emit()
    window.start_recording()
    window.thread.pause_remaining.emit(3)
    assert bar.isVisible() and bar.fractions() == (0, 1)


def test_idle_stop_tray_checkbox_syncs_with_settings_and_preserves_timeout(window, tmp_path):
    # GIVEN: автозавершение с настраиваемой длительностью.
    action = window.feature_actions["stop_on_idle"]
    assert action in window.tray.contextMenu().actions()
    assert action.text() == "Автозавершение без речи" and action.isChecked()
    # WHEN: сохраняем 90 секунд и меняем переключатель.
    window.preferences.idle_timeout_field.setValue(90)
    window.save_settings()
    action.trigger()
    # THEN: длительность сохраняется при выключении и включении.
    assert (
        not window.preferences.idle_stop_option.isChecked()
        and not window.preferences.idle_timeout_field.isEnabled()
    )
    saved = Settings.load(tmp_path)
    assert not saved.stop_on_idle and saved.idle_timeout == 90
    window.preferences.idle_stop_option.setChecked(True)
    assert action.isChecked() and window.preferences.idle_timeout_field.isEnabled()
    saved = Settings.load(tmp_path)
    assert saved.stop_on_idle and saved.idle_timeout == 90
    assert window.service.starts == window.service.closes == 0


def test_save_recordings_checkbox_syncs_persists_and_applies_to_next_session(window, qtbot, tmp_path):
    # GIVEN: сохранение аудио выключено.
    action = window.feature_actions["save_recordings"]
    assert action in window.tray.contextMenu().actions() and not action.isChecked()
    action.trigger()
    assert window.preferences.save_recordings_option.isChecked() and Settings.load(tmp_path).save_recordings
    assert window.service.starts == window.service.closes == 0
    # WHEN: включаем его перед диктовкой и выключаем во время неё.
    window.set_feature("auto_insert", False)
    window.ready = True
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    thread = window.thread
    window.preferences.save_recordings_option.setChecked(False)
    # THEN: текущая сессия сохраняет свой режим, следующая получает новый.
    assert not action.isChecked() and not Settings.load(tmp_path).save_recordings
    assert thread.settings.save_recordings and thread.stop_calls == 0
    thread.finished.emit()
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    assert not window.thread.settings.save_recordings


def test_recording_status_reports_saved_path_and_errors_without_losing_text(window, qtbot):
    # GIVEN: активная диктовка.
    # WHEN: получаем путь записи, ошибку диска и распознанный текст.
    window.set_feature("auto_insert", False)
    window.ready = True
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    thread = window.thread
    thread.event.emit({"type": "recording", "status": "started", "path": "/recordings/session"})
    # THEN: ошибка архива не прерывает диктовку и не теряет текст.
    assert "/recordings/session" in window.dictation.recording_info.text()
    window.thread.event.emit({"type": "recording", "status": "error", "message": "Диск заполнен"})
    assert "Диск заполнен" in window.dictation.recording_info.text()
    assert not window.last_error and window.thread.stop_calls == 0
    window.thread.event.emit({"type": "commit", "seq": 1, "delta": "Сохранённый текст."})
    window.thread.finished.emit()
    assert window.history.recent()[0][1] == "Сохранённый текст."
    thread.event.emit({"type": "recording", "status": "saved", "path": "/recordings/session"})
    assert window.dictation.recording_info.text() == "Аудиозапись сохранена: /recordings/session"


def test_recordings_button_opens_local_folder_and_is_disabled_for_remote_server(
    window, monkeypatch, tmp_path
):
    # GIVEN: локальный сервер и подменённое открытие файлового менеджера.
    opened = []
    monkeypatch.setattr(desktop.QDesktopServices, "openUrl", lambda url: opened.append(url) or True)
    # WHEN: открываем папку, затем задаём удалённый сервер.
    window.preferences.open_recordings_button.click()
    # THEN: локальная папка создаётся, для удалённого сервера кнопка недоступна.
    assert opened[0].toLocalFile() == str(tmp_path / "recordings")
    assert (tmp_path / "recordings").is_dir()
    window.preferences.url.setText("wss://example.com/v1/dictate")
    window.save_settings()
    assert not window.preferences.open_recordings_button.isEnabled()


def test_idle_stop_can_be_disabled_and_reenabled_during_dictation(window, qtbot):
    # GIVEN: активная диктовка с автозавершением.
    # WHEN: выключаем и снова включаем автозавершение.
    window.set_feature("auto_insert", False)
    window.ready = True
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    thread = window.thread
    window.feature_actions["stop_on_idle"].trigger()
    # THEN: старый сигнал не останавливает запись, новый отсчёт начинается полностью.
    assert thread.idle_changes == [False] and window.overlay.timeout_bar.isHidden()
    thread.idle_remaining.emit(0)
    thread.idle_expired.emit()
    assert thread.stop_calls == 0 and window.overlay.timeout_bar.isHidden()
    window.preferences.idle_stop_option.setChecked(True)
    assert thread.idle_changes == [False, True]
    assert window.overlay.timeout_bar.isVisible() and window.overlay.timeout_bar.fractions() == (1, 0)


def test_disabled_idle_stop_has_no_countdown_when_recording_starts(window, qtbot):
    # GIVEN: автозавершение выключено.
    window.set_feature("auto_insert", False)
    window.set_feature("stop_on_idle", False)
    window.ready = True
    # WHEN: запускаем диктовку.
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    # THEN: отсчёт автозавершения не показывается.
    assert window.overlay.isVisible() and window.overlay.timeout_bar.isHidden()
    assert not window.thread.settings.stop_on_idle


def test_idle_countdown_drains_resets_and_timeout_preserves_final_text(window, qtbot):
    # GIVEN: активная диктовка с тайм-аутом 45 секунд.
    # WHEN: получаем обновления таймера и его истечение.
    window.set_feature("auto_insert", False)
    window.ready = True
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    thread = window.thread
    # THEN: хвост сохраняется, следующая сессия получает полный отсчёт.
    assert window.overlay.timeout_bar.isVisible() and window.overlay.timeout_bar.fractions() == (1, 0)
    thread.idle_remaining.emit(22.5)
    assert window.overlay.timeout_bar.fractions() == (0.5, 0)
    assert not hasattr(window.overlay, "countdown")
    thread.idle_remaining.emit(45)
    assert window.overlay.timeout_bar.fractions() == (1, 0)
    thread.idle_remaining.emit(0)
    assert window.overlay.timeout_bar.fractions() == (0, 0)
    thread.idle_expired.emit()
    assert thread.stop_calls == 1 and not window.dictation.record.isEnabled()
    assert "Пауза 45 с" in window.overlay.state.text()
    thread.idle_remaining.emit(45)  # queued activity cannot restart a stopped countdown
    assert window.overlay.timeout_bar.isHidden()
    window.thread.event.emit({"type": "commit", "seq": 1, "delta": "Последние слова."})
    window.thread.event.emit({"type": "session_end", "text": "Последние слова."})
    thread.finished.emit()
    assert window.thread is None
    assert window.history.recent()[0][1] == QApplication.clipboard().text() == "Последние слова."
    qtbot.waitUntil(lambda: not window.overlay.isVisible(), timeout=2000)
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    assert window.overlay.timeout_bar.fractions() == (1, 0) and window.thread.stop_calls == 0


def test_hidden_overlay_still_stops_on_timeout_and_manual_stop_clears_countdown(window, qtbot):
    # GIVEN: диктовка со скрытым плавающим окном.
    window.set_feature("auto_insert", False)
    window.set_feature("show_overlay", False)
    window.ready = True
    # WHEN: таймер истекает, затем завершаем новую сессию вручную.
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    window.thread.idle_expired.emit()
    # THEN: оба способа завершают запись и скрывают отсчёт.
    assert window.thread.stop_calls == 1 and not window.overlay.isVisible()
    window.thread.finished.emit()
    window.set_feature("show_overlay", True)
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    window.toggle()
    assert window.overlay.timeout_bar.isHidden()
    window.toggle()
    assert window.thread.stop_calls == 1


def test_old_session_timeout_signals_cannot_stop_new_recording(window, qtbot):
    # GIVEN: новая активная запись и сигналы прежней сессии.
    window.set_feature("auto_insert", False)
    window.ready = True
    window.start_recording()
    old = window.thread
    old.finished.emit()
    window.start_recording()
    # WHEN: приходят устаревшие события таймеров.
    old.pause_remaining.emit(3)
    old.idle_remaining.emit(0)
    old.idle_expired.emit()
    # THEN: новая запись продолжает работу с полным отсчётом.
    assert window.thread.stop_calls == 0 and window.overlay.timeout_bar.fractions() == (1, 0)


def test_disabling_both_outputs_preserves_clipboard_and_keeps_history(window, qtbot):
    # GIVEN: автовставка и итоговое копирование выключены.
    window.set_feature("auto_insert", False)
    window.set_feature("copy_on_stop", False)
    window.set_feature("show_overlay", False)
    QApplication.clipboard().setText("Прежний буфер")
    window.ready = True
    # WHEN: диктовка получает текст и завершается.
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    window.thread.event.emit({"type": "commit", "seq": 1, "delta": "Новая диктовка."})
    window.thread.finished.emit()
    # THEN: буфер сохраняется, текст остаётся в истории.
    assert QApplication.clipboard().text() == "Прежний буфер"
    assert window.history.recent()[0][1] == "Новая диктовка."
    assert not window.overlay.isVisible()


@pytest.mark.desktop
@pytest.mark.skipif(
    not os.environ.get("TALK2G_DESKTOP_TESTS") or sys.platform != "linux", reason="Native X11 insertion"
)
def test_copy_disabled_restores_rich_clipboard_after_real_insertion(window, qtbot):
    # GIVEN: настоящее окно ввода и буфер с HTML, итоговое копирование выключено.
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
    # WHEN: распознанный текст вставляется и запись завершается.
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    window.thread.event.emit({"type": "commit", "seq": 1, "delta": "Введённый текст."})
    window.thread.finished.emit()  # the final clipboard update must await the paste queue
    qtbot.waitUntil(lambda: not window.inserter.busy, timeout=5000)
    qtbot.waitUntil(lambda: target.toPlainText() == "Введённый текст.", timeout=2000)
    # THEN: поле получает текст, прежний буфер восстанавливается после очереди вставки.
    assert QApplication.clipboard().text() == "Старый буфер"
    assert QApplication.clipboard().mimeData().html() == "<b>Старый буфер</b>"
    assert not window.overlay.isVisible()


@pytest.mark.desktop
@pytest.mark.skipif(
    not os.environ.get("TALK2G_DESKTOP_TESTS") or sys.platform != "linux", reason="Native X11 clipboard"
)
def test_external_clipboard_does_not_delay_opening_the_microphone(window, qtbot):
    # GIVEN: буфер принадлежит внешнему приложению.
    subprocess.run(["xclip", "-selection", "clipboard", "-in"], input="Чужой буфер", text=True, check=True)
    qtbot.wait(100)
    window.set_feature("auto_insert", False)
    window.ready = True
    began = time.monotonic()
    # WHEN: запускаем диктовку.
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None, timeout=1500)
    # THEN: микрофон открывается без задержки и прежний текст буфера сохранён.
    assert time.monotonic() - began < 1
    assert window.clipboard_before.text() == "Чужой буфер"


@pytest.mark.desktop
@pytest.mark.skipif(
    not os.environ.get("TALK2G_DESKTOP_TESTS") or sys.platform != "linux", reason="Native X11 insertion"
)
def test_auto_insert_can_be_disabled_and_reenabled_mid_session(window, qtbot):
    # GIVEN: настоящее целевое окно.
    target = QPlainTextEdit()
    qtbot.addWidget(target)
    target.show()
    target.setFocus()
    subprocess.run(["xdotool", "windowactivate", "--sync", str(int(target.winId()))], check=True)
    qtbot.waitUntil(lambda: target.isActiveWindow() and target.hasFocus())
    window.ready = True
    # WHEN: отключаем автовставку между фрагментами и снова включаем.
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    window.thread.event.emit({"type": "commit", "seq": 1, "delta": "Отключённая часть. "})
    window.feature_actions["auto_insert"].trigger()  # cancel a queued, not-yet-pasted delta
    qtbot.wait(300)
    # THEN: вводятся только разрешённые фрагменты, полный текст остаётся для копирования.
    assert target.toPlainText() == ""
    window.thread.event.emit({"type": "commit", "seq": 2, "delta": "Ещё отключённая часть. "})
    window.feature_actions["auto_insert"].trigger()
    window.thread.event.emit({"type": "commit", "seq": 3, "delta": "Включённая часть."})
    qtbot.waitUntil(lambda: target.toPlainText() == "Включённая часть.", timeout=5000)
    window.thread.finished.emit()
    qtbot.waitUntil(lambda: not window.inserter.busy, timeout=5000)
    assert QApplication.clipboard().text() == ("Отключённая часть. Ещё отключённая часть. Включённая часть.")


@pytest.mark.desktop
@pytest.mark.skipif(
    not os.environ.get("TALK2G_DESKTOP_TESTS") or sys.platform != "linux", reason="Native X11 insertion"
)
def test_voice_command_never_leaks_into_real_target_or_clipboard(window, qtbot):
    # GIVEN: настоящее поле ввода и включённая голосовая команда.
    target = QPlainTextEdit()
    qtbot.addWidget(target)
    target.show()
    target.setFocus()
    subprocess.run(["xdotool", "windowactivate", "--sync", str(int(target.winId()))], check=True)
    qtbot.waitUntil(lambda: target.isActiveWindow() and target.hasFocus())
    window.set_feature("stop_on_phrase", True)
    window.ready = True
    # WHEN: подтверждённая команда приходит двумя фрагментами.
    window.toggle()
    qtbot.waitUntil(lambda: window.thread is not None)
    thread = window.thread
    window.thread.event.emit({"type": "commit", "seq": 1, "delta": "Проверка. Конец"})
    qtbot.waitUntil(lambda: target.toPlainText() == "Проверка.", timeout=5000)
    window.thread.event.emit({"type": "commit", "seq": 2, "delta": " связи."})
    # THEN: команда останавливает запись и отсутствует в поле и итоговом буфере.
    assert thread.stop_calls == 1
    window.thread.event.emit({"type": "session_end", "text": "Проверка. Конец связи."})
    thread.finished.emit()
    qtbot.waitUntil(lambda: not window.inserter.busy, timeout=5000)
    assert target.toPlainText() == QApplication.clipboard().text() == "Проверка."
