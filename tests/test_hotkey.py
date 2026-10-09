import pytest

from talk2g.hotkey import parse_hotkey


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
