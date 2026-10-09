import ctypes
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QPlainTextEdit

from talk2g.desktop import InsertionQueue
from talk2g.hotkey import NativeHotkey
from talk2g.input import foreground, modifiers_pressed


def activate(widget, qtbot):
    widget.raise_()
    widget.activateWindow()
    widget.setFocus()
    qtbot.wait(100)
    if sys.platform == "linux":
        subprocess.run(
            ["xdotool", "windowactivate", "--sync", str(int(widget.winId()))], check=True, timeout=3
        )
    qtbot.waitUntil(lambda: foreground() == str(int(widget.winId())), timeout=3000)
    qtbot.waitUntil(lambda: widget.isActiveWindow() and widget.hasFocus(), timeout=3000)


@pytest.mark.desktop
@pytest.mark.skipif(
    not os.environ.get("TALK2G_DESKTOP_TESTS"), reason="Set TALK2G_DESKTOP_TESTS=1 for native insertion"
)
def test_native_unicode_input_into_own_window(qtbot):
    target = QPlainTextEdit()
    qtbot.addWidget(target)
    target.show()
    activate(target, qtbot)
    writer = InsertionQueue(foreground())
    failed = []
    writer.failed.connect(lambda message, text: failed.append(message))
    writer.add("Привет, мир! ")
    writer.add("Двадцать пять — 25.")
    qtbot.waitUntil(lambda: not writer.busy, timeout=5000)
    assert not failed, failed
    qtbot.wait(1000)
    assert target.toPlainText() == "Привет, мир! Двадцать пять — 25.", {
        "text": target.toPlainText(),
        "clipboard": QApplication.clipboard().text(),
        "window_focus": foreground(),
        "target": str(int(target.winId())),
    }


@pytest.mark.desktop
@pytest.mark.skipif(
    not os.environ.get("TALK2G_DESKTOP_TESTS"), reason="Set TALK2G_DESKTOP_TESTS=1 for native insertion"
)
def test_changed_window_suspends_insertion_and_preserves_remainder(qtbot):
    first, second = QPlainTextEdit(), QPlainTextEdit()
    for widget in (first, second):
        qtbot.addWidget(widget)
        widget.show()
    activate(first, qtbot)
    writer = InsertionQueue(foreground())
    activate(second, qtbot)
    failed = []
    writer.failed.connect(lambda message, text: failed.append(text))
    writer.add("Невведённый текст")
    assert writer.suspended
    assert failed == ["Невведённый текст"]
    assert first.toPlainText() == second.toPlainText() == ""


@pytest.mark.desktop
@pytest.mark.skipif(not os.environ.get("TALK2G_DESKTOP_TESTS") or sys.platform != "linux", reason="X11")
def test_native_hotkey_every_press_with_different_release_order_and_autorepeat(qtbot):
    target = QPlainTextEdit()
    qtbot.addWidget(target)
    target.show()
    activate(target, qtbot)
    activations = []
    hotkey = NativeHotkey("<ctrl>+<alt>+<space>", lambda: activations.append(True))
    hotkey.start()
    try:
        for index in range(8):
            subprocess.run(["xdotool", "keydown", "ctrl", "alt", "space"], check=True)
            qtbot.waitUntil(lambda expected=index + 1: len(activations) == expected, timeout=1500)
            qtbot.wait(700 if index == 0 else 80)
            assert len(activations) == index + 1  # holding the chord never toggles twice
            order = ("ctrl", "alt", "space") if index % 2 else ("space", "alt", "ctrl")
            subprocess.run(["xdotool", "keyup", *order], check=True)
            qtbot.wait(100)
        assert target.toPlainText() == ""  # the shortcut did not reach the target
    finally:
        subprocess.run(["xdotool", "keyup", "space", "ctrl", "alt"], check=True)
        hotkey.stop()


@pytest.mark.desktop
@pytest.mark.skipif(not os.environ.get("TALK2G_DESKTOP_TESTS") or sys.platform != "linux", reason="X11")
def test_ctrl_shift_a_in_english_and_russian_layouts(qtbot):
    from Xlib import XK, X, display
    from Xlib.ext import xtest

    target = QPlainTextEdit()
    qtbot.addWidget(target)
    target.show()
    activate(target, qtbot)
    connection = display.Display()
    rules = connection.screen().root.get_full_property(
        connection.intern_atom("_XKB_RULES_NAMES"), X.AnyPropertyType
    )
    layouts = rules.value.split(b"\0")[2].split(b",")
    if b"us" not in layouts or b"ru" not in layouts:
        connection.close()
        pytest.skip("Requires the user's us,ru keyboard map")
    codes = [
        connection.keysym_to_keycode(XK.string_to_keysym(name)) for name in ("Control_L", "Shift_L", "a")
    ]
    x11 = ctypes.CDLL("libX11.so.6")
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XkbGetState.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p]
    x11.XkbLockGroup.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    native = x11.XOpenDisplay(None)
    state = ctypes.create_string_buffer(128)  # group is the first unsigned byte of XkbStateRec
    assert x11.XkbGetState(native, 0x0100, state) == 0
    original_group = state.raw[0]
    activations = []
    hotkey = NativeHotkey("<ctrl>+<shift>+a", lambda: activations.append(True))
    hotkey.start()
    try:
        for layout in (b"us", b"ru"):
            assert x11.XkbLockGroup(native, 0x0100, layouts.index(layout))
            assert x11.XkbGetState(native, 0x0100, state) == 0
            assert state.raw[0] == layouts.index(layout)
            for index in range(4):
                before = len(activations)
                for code in codes:
                    xtest.fake_input(connection, X.KeyPress, code)
                connection.flush()
                qtbot.waitUntil(lambda before=before: len(activations) == before + 1, timeout=1500)
                qtbot.wait(700 if index == 0 else 50)
                assert len(activations) == before + 1
                for code in codes if index % 2 else reversed(codes):
                    xtest.fake_input(connection, X.KeyRelease, code)
                connection.sync()
                qtbot.wait(80)
        assert len(activations) == 8
        assert target.toPlainText() == ""
    finally:
        for code in reversed(codes):
            xtest.fake_input(connection, X.KeyRelease, code)
        connection.sync()
        hotkey.stop()
        x11.XkbLockGroup(native, 0x0100, original_group)
        x11.XkbGetState(native, 0x0100, state)
        x11.XCloseDisplay(native)
        connection.close()


@pytest.mark.desktop
@pytest.mark.skipif(not os.environ.get("TALK2G_DESKTOP_TESTS") or sys.platform != "linux", reason="X11")
@pytest.mark.parametrize("gap", [0, 0.004])
def test_fast_repeated_letter_presses_with_modifiers_held(qtbot, gap):
    from Xlib import XK, X, display
    from Xlib.ext import xtest

    target = QPlainTextEdit()
    qtbot.addWidget(target)
    target.show()
    activate(target, qtbot)
    connection = display.Display()
    codes = [
        connection.keysym_to_keycode(XK.string_to_keysym(name)) for name in ("Control_L", "Shift_L", "a")
    ]
    activations = []
    hotkey = NativeHotkey("<ctrl>+<shift>+a", lambda: activations.append(True))
    hotkey.start()
    try:
        for code in codes[:2]:
            xtest.fake_input(connection, X.KeyPress, code)
        for _ in range(20):
            xtest.fake_input(connection, X.KeyPress, codes[-1])
            connection.flush()
            time.sleep(gap)
            xtest.fake_input(connection, X.KeyRelease, codes[-1])
            connection.flush()
            time.sleep(gap)
        for code in reversed(codes[:2]):
            xtest.fake_input(connection, X.KeyRelease, code)
        connection.sync()
        qtbot.waitUntil(lambda: len(activations) == 20, timeout=3000)
        assert target.toPlainText() == ""
    finally:
        for code in reversed(codes):
            xtest.fake_input(connection, X.KeyRelease, code)
        connection.sync()
        hotkey.stop()
        connection.close()


@pytest.mark.desktop
@pytest.mark.skipif(not os.environ.get("TALK2G_DESKTOP_TESTS") or sys.platform != "linux", reason="X11")
def test_occupied_hotkey_reports_conflict_and_keeps_existing_binding(qtbot):
    target = QPlainTextEdit()
    qtbot.addWidget(target)
    target.show()
    activate(target, qtbot)
    activations = []
    first = NativeHotkey("<ctrl>+<alt>+<space>", lambda: activations.append(True))
    first.start()
    try:
        second = NativeHotkey("<ctrl>+<alt>+<space>", lambda: None)
        with pytest.raises(RuntimeError, match="занята"):
            second.start()
        subprocess.run(["xdotool", "key", "ctrl+alt+space"], check=True)
        qtbot.waitUntil(lambda: len(activations) == 1, timeout=1500)
    finally:
        first.stop()


@pytest.mark.desktop
@pytest.mark.skipif(not os.environ.get("TALK2G_DESKTOP_TESTS") or sys.platform != "linux", reason="X11")
def test_paste_waits_for_hotkey_release_without_corrupting_next_press(qtbot):
    target = QPlainTextEdit()
    qtbot.addWidget(target)
    target.show()
    activate(target, qtbot)
    activations = []
    hotkey = NativeHotkey("<ctrl>+<alt>+<space>", lambda: activations.append(True))
    hotkey.start()
    writer = InsertionQueue(foreground())
    try:
        subprocess.run(["xdotool", "keydown", "ctrl", "alt", "space"], check=True)
        qtbot.waitUntil(lambda: len(activations) == 1, timeout=1500)
        writer.add("Текст после отпускания клавиш.")
        qtbot.wait(250)
        assert writer.busy
        assert target.toPlainText() == ""
        subprocess.run(["xdotool", "keyup", "space", "alt", "ctrl"], check=True)
        qtbot.waitUntil(lambda: not writer.busy, timeout=5000)
        qtbot.waitUntil(lambda: target.toPlainText() == "Текст после отпускания клавиш.", timeout=1500)
        assert not modifiers_pressed()
        subprocess.run(["xdotool", "key", "ctrl+alt+space"], check=True)
        qtbot.waitUntil(lambda: len(activations) == 2, timeout=1500)
        qtbot.wait(100)
        assert not modifiers_pressed()
    finally:
        subprocess.run(["xdotool", "keyup", "space", "ctrl", "alt", "shift"], check=True)
        hotkey.stop()


@pytest.mark.desktop
@pytest.mark.skipif(not os.environ.get("TALK2G_DESKTOP_TESTS") or sys.platform != "linux", reason="X11")
def test_hotkey_during_continuous_pasting_never_loses_a_press(qtbot):
    target = QPlainTextEdit()
    qtbot.addWidget(target)
    target.show()
    activate(target, qtbot)
    activations = []
    errors = []
    hotkey = NativeHotkey("<ctrl>+<alt>+<space>", lambda: activations.append(True))
    hotkey.start()
    writer = InsertionQueue(foreground(), mode="shift_insert")
    writer.failed.connect(lambda message, text: errors.append(message))

    def press_repeatedly():
        try:
            for _ in range(16):
                subprocess.run(["xdotool", "key", "ctrl+alt+space"], check=True)
                time.sleep(0.04)
        except Exception as error:
            errors.append(str(error))

    thread = threading.Thread(target=press_repeatedly)
    try:
        for index in range(24):
            writer.add(f"Слово {index}. ")
        thread.start()
        qtbot.waitUntil(lambda: not thread.is_alive() and not writer.busy, timeout=10000)
        qtbot.waitUntil(lambda: len(activations) == 16, timeout=2000)
        expected = "".join(f"Слово {index}. " for index in range(24))
        qtbot.waitUntil(lambda: target.toPlainText() == expected, timeout=2000)
        assert not errors, errors
        assert not modifiers_pressed()
    finally:
        thread.join(timeout=5)
        subprocess.run(["xdotool", "keyup", "space", "ctrl", "alt", "shift"], check=True)
        hotkey.stop()


@pytest.mark.desktop
@pytest.mark.skipif(not os.environ.get("TALK2G_DESKTOP_TESTS") or sys.platform != "linux", reason="X11")
def test_native_progressive_paste_into_xfce_terminal(qtbot, tmp_path):
    output = tmp_path / "received.txt"
    receiver = Path(__file__).parent / "support/terminal_receiver.py"
    terminal = subprocess.Popen(
        [
            "xfce4-terminal",
            "--disable-server",
            "--title=talk2g terminal insertion test",
            "--dynamic-title-mode=none",
            "--execute",
            sys.executable,
            str(receiver),
            "--output",
            str(output),
            "--bracketed",
        ]
    )
    try:
        qtbot.waitUntil(lambda: output.with_suffix(".ready").exists(), timeout=5000)
        result = (
            subprocess.check_output(
                ["xdotool", "search", "--onlyvisible", "--name", "talk2g terminal insertion test"],
                text=True,
            )
            .strip()
            .splitlines()
        )
        target = result[-1]
        subprocess.run(["xdotool", "windowactivate", "--sync", target], check=True, timeout=3)
        qtbot.waitUntil(lambda: foreground() == target, timeout=3000)
        writer = InsertionQueue(target)
        failed = []
        writer.failed.connect(lambda message, text: failed.append(message))
        writer.add("Привет, ")
        qtbot.waitUntil(lambda: output.exists() and output.read_text() == "Привет, ", timeout=3000)
        writer.add("это терминал. ")
        writer.add("И последнее слово.")
        expected = "Привет, это терминал. И последнее слово."
        qtbot.waitUntil(lambda: not writer.busy, timeout=5000)
        assert not failed, failed
        qtbot.waitUntil(lambda: output.read_text() == expected, timeout=3000)
        assert output.read_text() == expected
        assert b"\x1b[200~" in output.with_suffix(".raw").read_bytes()
    finally:
        terminal.terminate()
        terminal.wait(timeout=5)
