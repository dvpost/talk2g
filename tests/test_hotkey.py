import sys
from types import SimpleNamespace

import pytest

from talk2g import xkb
from talk2g.hotkey import NativeHotkey, parse_hotkey


@pytest.mark.parametrize(
    "value, expected",
    [
        ("<ctrl>+<alt>+<space>", ({"ctrl", "alt"}, "space")),
        ("<ctrl>+<shift>+a", ({"ctrl", "shift"}, "a")),
        ("<control>+<shift>+f", ({"ctrl", "shift"}, "f")),
        ("<win>+F12", ({"cmd"}, "f12")),
    ],
)
def test_hotkey_syntax(value, expected):
    assert parse_hotkey(value) == expected


@pytest.mark.parametrize("value", ["space", "<ctrl>+<ctrl>+a", "<ctrl>+a+b", "<ctrl>+<oops>"])
def test_invalid_hotkey_is_reported(value):
    with pytest.raises(ValueError):
        parse_hotkey(value)


@pytest.mark.parametrize(
    ("present", "supported", "flags", "message"),
    [
        pytest.param(False, True, 1, "расширение XKB", id="missing-extension"),
        pytest.param(True, False, 1, "XKB 1.0", id="unsupported-version"),
        pytest.param(True, True, 0, "автоповтора", id="unsupported-repeat"),
    ],
)
@pytest.mark.skipif(sys.platform != "linux", reason="X11 is Linux-only")
def test_x11_hotkey_reports_missing_xkb_instead_of_using_another_repeat_algorithm(
    monkeypatch, present, supported, flags, message
):
    # GIVEN: X11 не предоставляет обязательную возможность XKB.
    from Xlib import display

    released, closed = [], []
    root = SimpleNamespace(ungrab_key=lambda *args: released.append(args))
    connection = SimpleNamespace(
        display=object(),
        query_extension=lambda name: SimpleNamespace(present=present, major_opcode=42),
        screen=lambda: SimpleNamespace(root=root),
        keysym_to_keycode=lambda symbol: 38,
        get_modifier_mapping=lambda: [[] for _ in range(8)],
        set_error_handler=lambda handler: None,
        sync=lambda: None,
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(xkb, "UseExtension", lambda **kwargs: SimpleNamespace(supported=supported))
    monkeypatch.setattr(xkb, "PerClientFlags", lambda **kwargs: SimpleNamespace(supported=flags, value=flags))
    monkeypatch.setattr(display, "Display", lambda: connection)
    key = NativeHotkey("<ctrl>+<shift>+a", lambda: pytest.fail("Unexpected activation"))
    # WHEN: регистрируем хоткей через публичный интерфейс.
    with pytest.raises(RuntimeError, match=message):
        key.start()
    # THEN: рабочий поток остановлен; ошибка не заменяется альтернативной регистрацией.
    assert not key.thread.is_alive()
    assert released and closed == [True]
