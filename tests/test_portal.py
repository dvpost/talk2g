import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("dbus_next", reason="Wayland DBus dependency is Linux-only")
from dbus_next import MessageType, Variant
from dbus_next import aio as dbus_aio

from talk2g.portal import WaylandPortal


@pytest.fixture
def portal(monkeypatch, tmp_path):
    # DBus — внешняя граница. Исполняются настоящие authorize/request/cleanup.
    monkeypatch.setenv("TALK2G_HOME", str(tmp_path))

    class Bus:
        unique_name = ":1.42"

        def __init__(self):
            self.calls = []
            self.handler = None
            self.failure = ""
            self.clipboard = True
            self.devices = 1
            self.shortcuts = True

        async def connect(self):
            return self

        def add_message_handler(self, handler):
            self.handler = handler

        def disconnect(self):
            pass

        async def call(self, message):
            operation = message.interface.rsplit(".", 1)[-1] + "." + message.member
            self.calls.append((operation, message.path))
            if operation == self.failure:
                return SimpleNamespace(message_type=MessageType.ERROR, body=["portal denied"])
            if message.member in ("CreateSession", "SelectDevices", "Start", "BindShortcuts"):
                result = {}
                if message.member == "CreateSession":
                    session = "/talk2g/shortcuts" if "GlobalShortcuts" in operation else "/talk2g/remote"
                    result["session_handle"] = Variant("o", session)
                elif message.member == "Start":
                    result = {
                        "devices": Variant("u", self.devices),
                        "clipboard_enabled": Variant("b", self.clipboard),
                    }
                elif message.member == "BindShortcuts":
                    result["shortcuts"] = Variant("a(sa{sv})", [["dictate", {}]] if self.shortcuts else [])
                token = message.body[-1]["handle_token"].value
                self.handler(
                    SimpleNamespace(
                        interface="org.freedesktop.portal.Request",
                        member="Response",
                        path=f"/org/freedesktop/portal/desktop/request/1_42/{token}",
                        body=[0, result],
                    )
                )
            return SimpleNamespace(message_type=MessageType.METHOD_RETURN, body=[])

    bus = Bus()
    monkeypatch.setattr(dbus_aio, "MessageBus", lambda **kwargs: bus)
    portal = WaylandPortal(lambda: None)
    try:
        yield portal, bus
    finally:
        portal.close()
        portal.thread.join(timeout=2)
        portal.loop.close()


async def test_wayland_authorization_and_clipboard_use_only_portal(portal):
    # GIVEN: все необходимые интерфейсы портала доступны.
    portal, bus = portal
    # WHEN: разрешаем ввод и копируем текст.
    result = await asyncio.to_thread(portal.authorize)
    portal.set_text("Текст.")
    # THEN: одна сессия ввода, одна сессия хоткея и один SetSelection без другого транспорта.
    assert result == "Ввод и горячая клавиша Wayland разрешены"
    operations = [operation for operation, path in bus.calls]
    assert operations.count("RemoteDesktop.CreateSession") == 1
    assert operations.count("GlobalShortcuts.CreateSession") == 1
    assert operations.count("Clipboard.SetSelection") == 1
    assert portal.text == "Текст.".encode() and portal.clipboard


@pytest.mark.parametrize(
    ("failure", "clipboard", "devices", "shortcuts", "message", "closed"),
    [
        pytest.param(
            "Clipboard.RequestClipboard",
            True,
            1,
            True,
            "portal denied",
            ["/talk2g/remote"],
            id="clipboard-interface-unavailable",
        ),
        pytest.param(
            "", False, 1, True, "буферу обмена", ["/talk2g/remote"], id="clipboard-permission-denied"
        ),
        pytest.param(
            "", True, 0, True, "клавиатурный ввод", ["/talk2g/remote"], id="keyboard-permission-denied"
        ),
        pytest.param(
            "GlobalShortcuts.CreateSession",
            True,
            1,
            True,
            "portal denied",
            ["/talk2g/remote"],
            id="global-shortcuts-unavailable",
        ),
        pytest.param(
            "GlobalShortcuts.BindShortcuts",
            True,
            1,
            True,
            "portal denied",
            ["/talk2g/remote", "/talk2g/shortcuts"],
            id="shortcut-binding-denied",
        ),
        pytest.param(
            "",
            True,
            1,
            False,
            "Хоткей не назначен",
            ["/talk2g/remote", "/talk2g/shortcuts"],
            id="shortcut-not-bound",
        ),
    ],
)
async def test_wayland_denial_is_explicit_and_closes_sessions_without_fallback(
    portal, failure, clipboard, devices, shortcuts, message, closed
):
    # GIVEN: обязательная возможность Wayland-портала недоступна или не разрешена.
    portal, bus = portal
    bus.failure, bus.clipboard, bus.devices, bus.shortcuts = failure, clipboard, devices, shortcuts
    # WHEN: оператор запрашивает разрешение ввода.
    with pytest.raises(RuntimeError, match=message):
        await asyncio.to_thread(portal.authorize)
    # THEN: отказ не превращается в успех с другим clipboard/хоткеем; все созданные сессии закрыты.
    assert [path for operation, path in bus.calls if operation == "Session.Close"] == closed
    assert portal.session == portal.shortcuts_session == "" and not portal.clipboard
    assert all(operation != "Clipboard.SetSelection" for operation, path in bus.calls)
    with pytest.raises(RuntimeError, match="буферу обмена"):
        portal.set_text("Текст.")
