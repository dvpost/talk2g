from __future__ import annotations

import ctypes
import os
import subprocess
import sys


def paste_shortcut(target: str, mode: str = "auto") -> str:
    modes = {"ctrl_v": "ctrl+v", "ctrl_shift_v": "ctrl+shift+v", "shift_insert": "shift+Insert"}
    if mode in modes:
        return modes[mode]
    from Xlib import display

    connection = display.Display()
    try:
        names = connection.create_resource_object("window", int(target)).get_wm_class() or ()
    finally:
        connection.close()
    terminals = {
        "xfce4-terminal",
        "gnome-terminal",
        "gnome-terminal-server",
        "konsole",
        "kitty",
        "alacritty",
        "wezterm",
        "org.wezfurlong.wezterm",
        "tilix",
        "terminator",
        "xterm",
        "uxterm",
        "urxvt",
        "rxvt",
        "st",
        "foot",
        "guake",
        "terminology",
    }
    return "ctrl+shift+v" if any(name.lower() in terminals for name in names) else "ctrl+v"


class FocusChanged(RuntimeError):
    pass


class ModifiersHeld(RuntimeError):
    pass


def x11_paste(target: str, shortcut: str) -> None:
    """Send one indivisible XTEST chord, without xdotool's per-key delays."""
    from Xlib import XK, X, display
    from Xlib.ext import xtest

    connection = display.Display()
    root = connection.screen().root
    names = {"ctrl": "Control_L", "shift": "Shift_L"}
    codes = [
        connection.keysym_to_keycode(XK.string_to_keysym(names.get(key, key))) for key in shortcut.split("+")
    ]
    if not all(codes):
        connection.close()
        raise RuntimeError("Клавиши вставки отсутствуют в раскладке X11")
    active_atom = connection.intern_atom("_NET_ACTIVE_WINDOW")
    connection.grab_server()
    try:
        # Recheck under the same server lock as the complete chord. Another
        # XTEST client cannot hold Shift halfway through our key sequence.
        active = root.get_full_property(active_atom, X.AnyPropertyType)
        if target:
            if active is None or not len(active.value):
                raise FocusChanged("Не удалось проверить активное окно X11. Текст сохранён для копирования")
            if str(active.value[0]) != target:
                raise FocusChanged("Активное окно изменилось. Оставшийся текст сохранён для копирования")
        mask = root.query_pointer().mask
        # EXC-0002: retry is safe only before XTEST input; see docs/exceptional_execution_paths.md.
        if mask & (X.ShiftMask | X.ControlMask | X.Mod1Mask | X.Mod4Mask | X.Mod5Mask):
            raise ModifiersHeld("Дождитесь отпускания клавиш")
        for code in codes:
            xtest.fake_input(connection, X.KeyPress, code)
        for code in reversed(codes):
            xtest.fake_input(connection, X.KeyRelease, code)
    finally:
        connection.ungrab_server()
        connection.sync()
        connection.close()


def modifiers_pressed() -> bool:
    """Wait for the user's hotkey release; never synthesize/restore held modifiers."""
    if sys.platform != "linux" or is_wayland():
        return False
    from Xlib import X, display

    connection = display.Display()
    try:
        mask = connection.screen().root.query_pointer().mask
        return bool(mask & (X.ShiftMask | X.ControlMask | X.Mod1Mask | X.Mod4Mask | X.Mod5Mask))
    finally:
        connection.close()


def is_wayland() -> bool:
    return sys.platform == "linux" and os.environ.get("XDG_SESSION_TYPE") == "wayland"


def foreground() -> str:
    if sys.platform == "win32":
        function = ctypes.windll.user32.GetForegroundWindow
        function.restype = ctypes.c_void_p
        return str(function())
    if sys.platform == "linux" and not is_wayland():
        result = subprocess.run(["xdotool", "getactivewindow"], capture_output=True, text=True, timeout=2)
        if result.returncode:
            raise RuntimeError("Не удалось определить активное окно X11")
        return result.stdout.strip()
    return ""


def check_focus(target: str) -> None:
    if target and foreground() != target:
        raise FocusChanged("Активное окно изменилось. Оставшийся текст сохранён для копирования")


def windows_unicode(text: str) -> None:
    """Explicit Win32 structures; handles Cyrillic and UTF-16 surrogate pairs."""

    class Keyboard(ctypes.Structure):
        _fields_ = [
            ("vk", ctypes.c_uint16),
            ("scan", ctypes.c_uint16),
            ("flags", ctypes.c_uint32),
            ("time", ctypes.c_uint32),
            ("extra", ctypes.c_size_t),
        ]

    class Mouse(ctypes.Structure):
        _fields_ = [
            ("dx", ctypes.c_int32),
            ("dy", ctypes.c_int32),
            ("data", ctypes.c_uint32),
            ("flags", ctypes.c_uint32),
            ("time", ctypes.c_uint32),
            ("extra", ctypes.c_size_t),
        ]

    class Payload(ctypes.Union):
        _fields_ = [("keyboard", Keyboard), ("mouse", Mouse)]

    class Input(ctypes.Structure):
        _fields_ = [("type", ctypes.c_uint32), ("payload", Payload)]

    encoded = text.encode("utf-16-le")
    units = [int.from_bytes(encoded[i : i + 2], "little") for i in range(0, len(encoded), 2)]
    events = (Input * (2 * len(units)))()
    for i, unit in enumerate(units):
        events[2 * i] = Input(1, Payload(keyboard=Keyboard(0, unit, 0x0004, 0, 0)))
        events[2 * i + 1] = Input(1, Payload(keyboard=Keyboard(0, unit, 0x0004 | 0x0002, 0, 0)))
    function = ctypes.windll.user32.SendInput
    function.argtypes = (ctypes.c_uint32, ctypes.POINTER(Input), ctypes.c_int)
    function.restype = ctypes.c_uint32
    if function(len(events), events, ctypes.sizeof(Input)) != len(events):
        raise RuntimeError("Windows заблокировала ввод. Проверьте уровень прав целевого приложения")


def paste(target: str, portal=None, mode: str = "auto") -> None:
    check_focus(target)
    if sys.platform == "win32":
        raise RuntimeError("Для Windows используйте Unicode-ввод")
    if is_wayland():
        if portal is None:
            raise RuntimeError("В настройках сначала разрешите ввод через портал Wayland")
        portal.paste()
    else:
        shortcut = paste_shortcut(target, mode)
        x11_paste(target, shortcut)
