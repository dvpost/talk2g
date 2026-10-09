from __future__ import annotations

import ctypes
import logging
import select
import sys
import threading

log = logging.getLogger(__name__)


def parse_hotkey(value: str) -> tuple[set[str], str]:
    parts = [part.strip().strip("<>").lower() for part in value.split("+")]
    aliases = {"control": "ctrl", "super": "cmd", "win": "cmd"}
    parts = [aliases.get(part, part) for part in parts]
    modifiers = {part for part in parts if part in ("ctrl", "alt", "shift", "cmd")}
    keys = [part for part in parts if part not in modifiers]
    if len(parts) != len(set(parts)) or len(keys) != 1 or not modifiers:
        raise ValueError("Хоткей: например <ctrl>+<shift>+a")
    key = keys[0]
    names = {"space", "tab", "enter", "esc", "escape", "backspace", "delete", "insert", "home", "end"}
    if not (len(key) == 1 and key.isascii() and key.isalnum()) and key not in names:
        if not (key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24):
            raise ValueError("Хоткей: используйте латинскую букву, цифру, Space или F1–F24")
    return modifiers, key


class NativeHotkey:
    """An OS-owned shortcut: no layout-dependent listener state or app shortcut leakage."""

    def __init__(self, value: str, callback):
        self.modifiers, self.key = parse_hotkey(value)
        self.callback = callback
        self.stopping = threading.Event()
        self.ready = threading.Event()
        self.error = None
        self.thread_id = None
        self.thread = threading.Thread(target=self._run, name="native-hotkey", daemon=True)

    def start(self):
        self.thread.start()
        if not self.ready.wait(5):
            self.stop()
            raise RuntimeError("Не удалось зарегистрировать горячую клавишу")
        if self.error:
            self.stop()
            raise self.error

    def stop(self):
        self.stopping.set()
        if sys.platform == "win32" and self.thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)
        if self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(timeout=2)

    def _run(self):
        try:
            if sys.platform == "win32":
                self._windows()
            else:
                self._x11()
        except Exception as error:
            self.error = error
            log.exception("Горячая клавиша недоступна")
        finally:
            self.ready.set()

    def _x11(self):
        from Xlib import XK, X, display

        from .xkb import enable_detectable_repeat

        connection = display.Display()
        root = connection.screen().root
        names = {
            "enter": "Return",
            "esc": "Escape",
            "escape": "Escape",
            "backspace": "BackSpace",
            "delete": "Delete",
            "insert": "Insert",
            "home": "Home",
            "end": "End",
            "tab": "Tab",
        }
        function_key = self.key.startswith("f") and self.key[1:].isdigit()
        name = names.get(self.key, self.key.upper() if function_key else self.key)
        code = connection.keysym_to_keycode(XK.string_to_keysym(name))
        if not code:
            connection.close()
            raise ValueError("Клавиша отсутствует в раскладке X11")
        masks = {"ctrl": X.ControlMask, "alt": X.Mod1Mask, "shift": X.ShiftMask, "cmd": X.Mod4Mask}
        modifiers = sum(masks[part] for part in self.modifiers)
        locks = {0, X.LockMask}
        mapping = connection.get_modifier_mapping()
        for symbol in ("Num_Lock", "Scroll_Lock"):
            lock_code = connection.keysym_to_keycode(XK.string_to_keysym(symbol))
            for index, codes in enumerate(mapping):
                if lock_code and lock_code in codes:
                    locks |= {value | (1 << index) for value in list(locks)}
        variants = {modifiers | lock for lock in locks}
        ignored_locks = 0
        for lock in locks:
            ignored_locks |= lock
        modifier_filter = 0xFF & ~ignored_locks
        errors = []
        connection.set_error_handler(lambda error, request: errors.append(error))
        try:
            enable_detectable_repeat(connection)
            root.change_attributes(event_mask=X.KeyPressMask | X.KeyReleaseMask)
            for mask in variants:
                root.grab_key(code, mask, False, X.GrabModeAsync, X.GrabModeAsync)
            connection.sync()
            if errors:
                raise RuntimeError("Горячая клавиша занята другой программой. Выберите другую в настройках")
            self.ready.set()
            held = False
            while not self.stopping.is_set():
                if not connection.pending_events():
                    select.select([connection.fileno()], [], [], 0.04)
                while connection.pending_events():
                    event = connection.next_event()
                    if event.type == X.KeyRelease and event.detail == code:
                        held = False
                    if (
                        event.type == X.KeyPress
                        and event.detail == code
                        and event.state & modifier_filter == modifiers
                    ):
                        log.info("X11 хоткей: модификаторы=%s, удерживается=%s", event.state, held)
                        if not held:
                            held = True
                            self.callback()
                if held:
                    # Read actual key state. Auto-repeat and missed/translated key-up
                    # events cannot leave the next physical press stuck in a latch.
                    keys = connection.query_keymap()
                    if not keys[code // 8] & (1 << (code % 8)):
                        held = False
        finally:
            for mask in variants:
                root.ungrab_key(code, mask)
            connection.sync()
            connection.close()

    def _windows(self):
        from ctypes import wintypes

        user = ctypes.windll.user32
        self.thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        message = wintypes.MSG()
        user.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
        masks = {"alt": 1, "ctrl": 2, "shift": 4, "cmd": 8}
        modifiers = sum(masks[part] for part in self.modifiers) | 0x4000  # MOD_NOREPEAT
        special = {
            "space": 0x20,
            "tab": 0x09,
            "enter": 0x0D,
            "esc": 0x1B,
            "escape": 0x1B,
            "backspace": 0x08,
            "delete": 0x2E,
            "insert": 0x2D,
            "home": 0x24,
            "end": 0x23,
        }
        key = special.get(self.key)
        if key is None:
            function_key = self.key.startswith("f") and self.key[1:].isdigit()
            key = 0x70 + int(self.key[1:]) - 1 if function_key else ord(self.key.upper())
        if not user.RegisterHotKey(None, 1, modifiers, key):
            raise RuntimeError("Горячая клавиша занята другой программой. Выберите другую в настройках")
        try:
            self.ready.set()
            while not self.stopping.is_set():
                result = user.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    break
                if message.message == 0x0312:  # WM_HOTKEY
                    self.callback()
        finally:
            user.UnregisterHotKey(None, 1)
