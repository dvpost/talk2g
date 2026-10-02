from __future__ import annotations

import asyncio
import os
import threading
import uuid

from .config import project_home


class WaylandPortal:
    """Native desktop permission and clipboard transfer, no root/uinput daemon."""

    destination = "org.freedesktop.portal.Desktop"
    path = "/org/freedesktop/portal/desktop"

    def __init__(self, toggle_callback):
        self.callback = toggle_callback
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True, name="wayland-portal")
        self.thread.start()
        self.bus = None
        self.requests = {}
        self.session = ""
        self.shortcuts_session = ""
        self.clipboard = False
        self.text = b""

    def authorize(self) -> str:
        return asyncio.run_coroutine_threadsafe(self._authorize(), self.loop).result(timeout=180)

    async def _call(self, interface, member, signature, body):
        from dbus_next import Message, MessageType

        response = await self.bus.call(
            Message(
                destination=self.destination,
                path=self.path,
                interface=f"org.freedesktop.portal.{interface}",
                member=member,
                signature=signature,
                body=body,
            )
        )
        if response.message_type == MessageType.ERROR:
            raise RuntimeError(" ".join(str(item) for item in response.body))
        return response

    def _signal(self, message):
        if message.interface == "org.freedesktop.portal.Request" and message.member == "Response":
            future = self.requests.get(message.path)
            if future and not future.done():
                future.set_result(message.body)
        elif message.interface == "org.freedesktop.portal.GlobalShortcuts" and message.member == "Activated":
            if message.body[0] == self.shortcuts_session and message.body[1] == "dictate":
                self.callback()
        elif (
            message.interface == "org.freedesktop.portal.Clipboard" and message.member == "SelectionTransfer"
        ):
            if message.body[0] == self.session:
                self.loop.create_task(self._transfer(message.body[1], message.body[2], self.text))

    async def _request(self, interface, member, signature, body):
        from dbus_next import Variant

        token = "giga" + uuid.uuid4().hex
        body[-1]["handle_token"] = Variant("s", token)
        sender = self.bus.unique_name.lstrip(":").replace(".", "_")
        handle = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        future = self.loop.create_future()
        self.requests[handle] = future
        try:
            await self._call(interface, member, signature, body)
            code, result = await asyncio.wait_for(future, 120)
            if code:
                raise RuntimeError("Разрешение Wayland не предоставлено")
            return result
        finally:
            self.requests.pop(handle, None)

    async def _authorize(self):
        try:
            return await self._authorize_impl()
        except Exception:
            if self.session:
                from dbus_next import Message

                await self.bus.call(
                    Message(
                        destination=self.destination,
                        path=self.session,
                        interface="org.freedesktop.portal.Session",
                        member="Close",
                    )
                )
            self.session = ""
            self.clipboard = False
            raise

    async def _authorize_impl(self):
        from dbus_next import Message, Variant
        from dbus_next.aio import MessageBus

        if self.bus is None:
            self.bus = await MessageBus(negotiate_unix_fd=True).connect()
            self.bus.add_message_handler(self._signal)
            await self.bus.call(
                Message(
                    destination="org.freedesktop.DBus",
                    path="/org/freedesktop/DBus",
                    interface="org.freedesktop.DBus",
                    member="AddMatch",
                    signature="s",
                    body=["type='signal',sender='org.freedesktop.portal.Desktop'"],
                )
            )
        if not self.session:
            result = await self._request(
                "RemoteDesktop",
                "CreateSession",
                "a{sv}",
                [{"session_handle_token": Variant("s", "giga" + uuid.uuid4().hex)}],
            )
            self.session = result["session_handle"].value
            options = {"types": Variant("u", 1), "persist_mode": Variant("u", 2)}
            token_path = project_home() / ".data/wayland-token"
            if token_path.exists():
                options["restore_token"] = Variant("s", token_path.read_text().strip())
            await self._request("RemoteDesktop", "SelectDevices", "oa{sv}", [self.session, options])
            try:
                await self._call("Clipboard", "RequestClipboard", "oa{sv}", [self.session, {}])
            except RuntimeError:
                pass
            result = await self._request("RemoteDesktop", "Start", "osa{sv}", [self.session, "", {}])
            if not result["devices"].value & 1:
                self.session = ""
                raise RuntimeError("Портал не разрешил клавиатурный ввод")
            self.clipboard = bool(result.get("clipboard_enabled", Variant("b", False)).value)
            if restore := result.get("restore_token"):
                token_path.parent.mkdir(parents=True, exist_ok=True)
                token_path.write_text(restore.value)
                token_path.chmod(0o600)
        if not self.shortcuts_session:
            try:
                result = await self._request(
                    "GlobalShortcuts",
                    "CreateSession",
                    "a{sv}",
                    [{"session_handle_token": Variant("s", "giga" + uuid.uuid4().hex)}],
                )
                self.shortcuts_session = result["session_handle"].value
                shortcut = [
                    [
                        "dictate",
                        {
                            "description": Variant("s", "Начать/остановить диктовку"),
                            "preferred_trigger": Variant("s", "CTRL+ALT+space"),
                        },
                    ]
                ]
                bound = await self._request(
                    "GlobalShortcuts",
                    "BindShortcuts",
                    "oa(sa{sv})sa{sv}",
                    [self.shortcuts_session, shortcut, "", {}],
                )
                shortcuts = bound.get("shortcuts", Variant("a(sa{sv})", [])).value
                if not any(item[0] == "dictate" for item in shortcuts):
                    raise RuntimeError("Хоткей не назначен")
            except RuntimeError:
                self.shortcuts_session = ""
                return "Ввод разрешён. Назначьте в настройках Linux хоткей на run-linux.sh toggle"
        return "Ввод и горячая клавиша Wayland разрешены"

    async def _transfer(self, mime, serial, text):
        descriptor = None
        success = False
        try:
            if mime not in ("text/plain;charset=utf-8", "text/plain"):
                return
            response = await self._call("Clipboard", "SelectionWrite", "ou", [self.session, serial])
            descriptor = response.unix_fds[response.body[0]]

            # Writes run outside the DBus loop so a slow clipboard reader cannot freeze it.
            def write():
                offset = 0
                while offset < len(text):
                    offset += os.write(descriptor, text[offset:])

            await asyncio.to_thread(write)
            success = True
        finally:
            if descriptor is not None:
                os.close(descriptor)
            await self._call("Clipboard", "SelectionWriteDone", "oub", [self.session, serial, success])

    def set_text(self, text: str):
        if not self.clipboard:
            return False
        from dbus_next import Variant

        async def setting():
            self.text = text.encode("utf-8")
            await self._call(
                "Clipboard",
                "SetSelection",
                "oa{sv}",
                [self.session, {"mime_types": Variant("as", ["text/plain;charset=utf-8", "text/plain"])}],
            )

        asyncio.run_coroutine_threadsafe(setting(), self.loop).result(timeout=3)
        return True

    def paste(self):
        if not self.session:
            raise RuntimeError("Сначала разрешите ввод через портал Wayland")

        async def pressing():
            for code, state in ((29, 1), (47, 1), (47, 0), (29, 0)):
                await self._call(
                    "RemoteDesktop", "NotifyKeyboardKeycode", "oa{sv}iu", [self.session, {}, code, state]
                )

        asyncio.run_coroutine_threadsafe(pressing(), self.loop).result(timeout=3)

    def close(self):
        if self.bus:
            self.loop.call_soon_threadsafe(self.bus.disconnect)
        self.loop.call_soon_threadsafe(self.loop.stop)
