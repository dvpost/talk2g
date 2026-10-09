from dataclasses import asdict, replace

import pytest
import sounddevice
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

from talk2g.config import Settings
from talk2g.history import History
from talk2g.ui.history import HistoryPage
from talk2g.ui.settings import SettingsPage


@pytest.fixture
def devices(monkeypatch):
    monkeypatch.setattr(
        sounddevice,
        "query_devices",
        lambda: [
            {"name": "Микрофон", "max_input_channels": 1},
            {"name": "Динамики", "max_input_channels": 0},
        ],
    )


@pytest.fixture
def history(tmp_path):
    history = History(tmp_path)
    yield history
    history.close()


def test_settings_form_changes_visible_values_and_preserves_other_settings(qtbot, devices):
    # GIVEN: настройки модели и числа потоков, которые не показываются в форме.
    current = Settings(model="gigaam-v3-e2e-rnnt", threads=8)
    original = asdict(current)
    page = SettingsPage(current)
    qtbot.addWidget(page)
    # WHEN: пользователь выбирает микрофон, меняет сервер, токен и длительности пауз.
    page.microphone.setCurrentIndex(page.microphone.findData("Микрофон"))
    page.url.setText(" wss://example.com/v1/dictate ")
    page.token.setText(" token ")
    page.idle_timeout_field.setValue(90)
    page.recognition_pause_field.setValue(5)
    edited = page.values(current)
    # THEN: меняются только выбранные поля, скрытые настройки и исходный объект сохраняются.
    assert asdict(edited) == {
        **original,
        "microphone": "Микрофон",
        "server_url": "wss://example.com/v1/dictate",
        "token": "token",
        "idle_timeout": 90,
        "recognition_pause": 5,
    }
    assert asdict(current) == original


def test_settings_sync_does_not_emit_user_changes_but_checkbox_click_does(qtbot, devices):
    # GIVEN: форма с подписчиками пользовательских изменений.
    page = SettingsPage(Settings())
    qtbot.addWidget(page)
    features, positions = [], []
    page.feature_changed.connect(lambda name, value: features.append((name, value)))
    page.position_changed.connect(lambda: positions.append(page.overlay_position_field.currentData()))
    # WHEN: контроллер синхронизирует состояние после действия в трее, затем пользователь нажимает галочку.
    page.sync(replace(Settings(), show_overlay=False, stop_on_idle=False, overlay_position="bottom_left"))
    page.overlay_option.click()
    # THEN: синхронизация не создаёт повторные изменения, только клик отправляет намерение пользователя.
    assert features == [("show_overlay", True)] and positions == []
    assert not page.idle_timeout_field.isEnabled()
    assert page.overlay_position_field.currentData() == "bottom_left"


@pytest.mark.parametrize("enumeration_fails", [False, True], ids=["disconnected", "enumeration-error"])
def test_unavailable_selected_microphone_is_preserved_on_settings_save(
    qtbot, devices, monkeypatch, enumeration_fails
):
    # GIVEN: сохранён выбранный микрофон, который система сейчас не возвращает.
    if enumeration_fails:

        def denied():
            raise RuntimeError("PortAudio unavailable")

        monkeypatch.setattr(sounddevice, "query_devices", denied)
    current = Settings(microphone="Выбранный USB микрофон")
    page = SettingsPage(current)
    qtbot.addWidget(page)
    # WHEN: пользователь меняет длительность паузы и сохраняет форму.
    page.recognition_pause_field.setValue(5)
    edited = page.values(current)
    # THEN: приложение не заменяет микрофон системным по умолчанию.
    assert edited.microphone == current.microphone
    assert edited.recognition_pause == 5
    assert "недоступен" in page.microphone.currentText()
    assert bool(page.microphone_error) == enumeration_fails


def test_history_selection_copies_full_text_and_refresh_removes_stale_selection(qtbot, history):
    # GIVEN: две записи в настоящем локальном хранилище.
    history.add("Первый абзац.")
    history.add("Второй абзац.\nПродолжение второго абзаца.")
    page = HistoryPage(history)
    qtbot.addWidget(page)
    # WHEN: пользователь выбирает вторую запись и копирует её целиком.
    page.history_list.setCurrentRow(0)
    copy = next(
        button for button in page.findChildren(QPushButton) if button.text() == "Скопировать выбранную запись"
    )
    copy.click()
    # THEN: буфер содержит полный текст выбранной записи, обновление после очистки убирает старый текст.
    assert (page.history_list.count(), page.history_text.toPlainText(), QApplication.clipboard().text()) == (
        2,
        "Второй абзац.\nПродолжение второго абзаца.",
        "Второй абзац.\nПродолжение второго абзаца.",
    )
    history.clear()
    page.refresh()
    assert page.history_list.count() == 0 and page.history_text.toPlainText() == ""


@pytest.mark.parametrize("confirmed", [True, False], ids=["confirmed", "declined"])
def test_history_is_cleared_only_after_confirmation(qtbot, history, monkeypatch, confirmed):
    # GIVEN: запись в истории и выбранный ответ пользователя на подтверждение.
    history.add("Сохранённый текст.")
    page = HistoryPage(history)
    qtbot.addWidget(page)
    page.history_list.setCurrentRow(0)
    answer = QMessageBox.StandardButton.Yes if confirmed else QMessageBox.StandardButton.No
    monkeypatch.setattr(QMessageBox, "question", lambda *args: answer)
    # WHEN: пользователь нажимает кнопку очистки.
    clear = next(button for button in page.findChildren(QPushButton) if button.text() == "Очистить историю")
    clear.click()
    # THEN: согласие очищает хранилище и форму, отказ сохраняет оба.
    expected = [] if confirmed else ["Сохранённый текст."]
    assert [text for _, text in history.recent()] == expected
    assert page.history_list.count() == len(expected)
    assert page.history_text.toPlainText() == ("" if confirmed else "Сохранённый текст.")
