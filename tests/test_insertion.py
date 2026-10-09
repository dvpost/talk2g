from collections import deque
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from talk2g import insertion
from talk2g.input import FocusChanged, ModifiersHeld, x11_paste


@pytest.fixture
def environment(monkeypatch, qapp):
    callbacks = deque()
    now, pasted = [0.0], []
    monkeypatch.setattr(
        insertion, "QTimer", SimpleNamespace(singleShot=lambda delay, callback: callbacks.append(callback))
    )
    monkeypatch.setattr(insertion, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(insertion, "check_focus", lambda target: None)
    monkeypatch.setattr(insertion, "is_wayland", lambda: False)
    monkeypatch.setattr(insertion.sys, "platform", "linux")
    monkeypatch.setattr(insertion, "modifiers_pressed", lambda: False)
    monkeypatch.setattr(insertion, "paste", lambda *args: pasted.append(QApplication.clipboard().text()))
    QApplication.clipboard().setText("Исходный буфер")
    return SimpleNamespace(callbacks=callbacks, now=now, pasted=pasted)


def test_held_modifiers_release_before_one_paste(environment, monkeypatch):
    # GIVEN: горячая клавиша ещё зажата, два фрагмента ожидают системного ввода.
    held = [True]
    monkeypatch.setattr(insertion, "modifiers_pressed", lambda: held[0])
    writer = insertion.InsertionQueue("target")
    errors = []
    writer.failed.connect(lambda message, text: errors.append((message, text)))
    writer.add("Первый.")
    writer.add(" Второй.")
    environment.callbacks.popleft()()
    assert environment.pasted == []
    # WHEN: пользователь отпускает клавиши в пределах 5 секунд.
    environment.now[0], held[0] = 4.9, False
    while environment.callbacks:
        environment.callbacks.popleft()()
    # THEN: каждый фрагмент вставлен один раз и в исходном порядке.
    assert environment.pasted == ["Первый.", " Второй."]
    assert errors == [] and not writer.busy


@pytest.mark.parametrize("race", [False, True], ids=["held-before-paste", "held-under-x11-lock"])
def test_modifier_deadline_preserves_remainder_without_keyboard_input(environment, monkeypatch, race):
    # GIVEN: модификаторы не отпускаются, в очереди находятся два фрагмента.
    attempts = []
    if race:

        def locked(*args):
            attempts.append(True)
            raise ModifiersHeld("Дождитесь отпускания клавиш")

        monkeypatch.setattr(insertion, "paste", locked)
    else:
        monkeypatch.setattr(insertion, "modifiers_pressed", lambda: True)
    writer = insertion.InsertionQueue("target")
    errors = []
    writer.failed.connect(lambda message, text: errors.append((message, text)))
    writer.add("Первый.")
    writer.add(" Второй.")
    environment.callbacks.popleft()()
    # WHEN: проходит больше 5 секунд без допустимого состояния клавиш.
    environment.now[0] = 5.1
    environment.callbacks.popleft()()
    # THEN: очередь приостановлена с полным остатком, новых попыток больше нет.
    assert len(errors) == 1 and errors[0][1] == "Первый. Второй."
    assert environment.pasted == [] and not environment.callbacks
    assert len(attempts) == (2 if race else 0)
    assert writer.suspended and not writer.busy


@pytest.mark.parametrize(
    "active", [None, SimpleNamespace(value=[])], ids=["missing-property", "empty-property"]
)
def test_unverifiable_x11_focus_does_not_send_keyboard_events(monkeypatch, active):
    # GIVEN: сервер X11 не предоставляет достоверное активное окно под lock.
    from Xlib import display
    from Xlib.ext import xtest

    events, cleanup = [], []
    root = SimpleNamespace(get_full_property=lambda *args: active)
    connection = SimpleNamespace(
        screen=lambda: SimpleNamespace(root=root),
        keysym_to_keycode=lambda symbol: 1,
        intern_atom=lambda name: 1,
        grab_server=lambda: None,
        ungrab_server=lambda: cleanup.append("unlock"),
        sync=lambda: None,
        close=lambda: cleanup.append("close"),
    )
    monkeypatch.setattr(display, "Display", lambda: connection)
    monkeypatch.setattr(xtest, "fake_input", lambda *args: events.append(args))
    # WHEN: пытаемся вставить в конкретное целевое окно.
    with pytest.raises(FocusChanged, match="проверить активное окно"):
        x11_paste("target", "ctrl+v")
    # THEN: неопределённый фокус не считается разрешением ввода, ресурсы освобождены.
    assert events == [] and cleanup == ["unlock", "close"]


@pytest.mark.parametrize("mask", [1, 4, 8, 64, 128], ids=["shift", "ctrl", "alt", "super", "altgr"])
def test_x11_held_modifiers_are_rejected_before_any_keyboard_event(monkeypatch, mask):
    # GIVEN: модификатор зажат при проверке непосредственно под блокировкой X11.
    from Xlib import display
    from Xlib.ext import xtest

    events, cleanup = [], []
    root = SimpleNamespace(
        get_full_property=lambda *args: SimpleNamespace(value=[123]),
        query_pointer=lambda: SimpleNamespace(mask=mask),
    )
    connection = SimpleNamespace(
        screen=lambda: SimpleNamespace(root=root),
        keysym_to_keycode=lambda symbol: 1,
        intern_atom=lambda name: 1,
        grab_server=lambda: None,
        ungrab_server=lambda: cleanup.append("unlock"),
        sync=lambda: None,
        close=lambda: cleanup.append("close"),
    )
    monkeypatch.setattr(display, "Display", lambda: connection)
    monkeypatch.setattr(xtest, "fake_input", lambda *args: events.append(args))
    # WHEN: начинаем системную вставку в подтверждённое целевое окно.
    with pytest.raises(ModifiersHeld, match="отпускания клавиш"):
        x11_paste("123", "ctrl+v")
    # THEN: повтор допустим, потому что ни одна клавиша не отправлена; lock освобождён.
    assert events == [] and cleanup == ["unlock", "close"]


def test_failed_keyboard_input_is_never_retried(environment, monkeypatch):
    # GIVEN: системный ввод завершается ошибкой, которая могла возникнуть после побочного эффекта.
    attempts = []

    def failed(*args):
        attempts.append(True)
        raise RuntimeError("input blocked")

    monkeypatch.setattr(insertion, "paste", failed)
    writer = insertion.InsertionQueue("target")
    errors = []
    writer.failed.connect(lambda message, text: errors.append((message, text)))
    writer.add("Текст.")
    # WHEN: происходит одна попытка ввода.
    environment.callbacks.popleft()()
    # THEN: ошибка явная, текст сохранён, автоматического повтора нет.
    assert attempts == [True] and errors == [("input blocked", "Текст.")]
    assert not environment.callbacks and writer.suspended


def test_wayland_without_portal_does_not_use_another_clipboard(environment, monkeypatch):
    # GIVEN: Wayland без разрешённого портала и пользовательский буфер.
    monkeypatch.setattr(insertion, "is_wayland", lambda: True)
    writer = insertion.InsertionQueue("")
    errors = []
    writer.failed.connect(lambda message, text: errors.append((message, text)))
    # WHEN: запрашиваем вставку фрагмента.
    writer.add("Текст.")
    # THEN: отказ не меняет буфер и не запускает альтернативную вставку.
    assert errors == [("В настройках сначала разрешите ввод через портал Wayland", "Текст.")]
    assert QApplication.clipboard().text() == "Исходный буфер"
    assert environment.pasted == [] and not environment.callbacks
