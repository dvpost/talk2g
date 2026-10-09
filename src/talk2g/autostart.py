"""Current-user login startup on Linux (XDG) and Windows (HKCU Run)."""

from __future__ import annotations

import configparser
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import project_home

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "talk2g"


def desktop_argument(value: str) -> str:
    if any(character in value for character in "\n\r\0"):
        raise ValueError("Путь автозапуска содержит недопустимый символ")
    # Desktop string escaping is undone before Exec argument unquoting.
    value = value.replace("%", "%%")
    value = "".join("\\" + character if character in '\\"`$' else character for character in value)
    return '"' + value.replace("\\", "\\\\") + '"'


class Autostart:
    def __init__(
        self,
        *,
        home: Path | None = None,
        config_dir: Path | None = None,
        platform: str | None = None,
        executable: str | None = None,
        frozen: bool | None = None,
        registry=None,
    ):
        self.home = (home or project_home()).resolve()
        self.platform = platform or sys.platform
        self.executable = Path(executable or sys.executable)  # preserve virtualenv interpreter symlinks
        self.frozen = getattr(sys, "frozen", False) if frozen is None else frozen
        self.config_dir = config_dir or Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        self.entry = self.config_dir / "autostart/talk2g.desktop"
        self.registry = registry

    def command(self) -> list[str]:
        executable = self.executable
        if self.platform == "win32" and not self.frozen:
            executable = executable.with_name("pythonw.exe")
            if not executable.is_file():
                raise RuntimeError("Для автозапуска Windows нужен pythonw.exe рядом с Python")
        prefix = [str(executable)] if self.frozen else [str(executable), "-m", "talk2g"]
        return prefix + ["app", "--background", "--home", str(self.home)]

    def _windows_registry(self):
        if self.registry is None:
            import winreg

            self.registry = winreg
        return self.registry

    def is_enabled(self) -> bool:
        if self.platform == "linux":
            parser = configparser.ConfigParser(interpolation=None)
            try:
                parser.read_string(self.entry.read_text(encoding="utf-8"))
                section = parser["Desktop Entry"]
                return (
                    section.get("Type") == "Application"
                    and bool(section.get("Exec"))
                    and not section.getboolean("Hidden", fallback=False)
                    and section.getboolean("X-GNOME-Autostart-enabled", fallback=True)
                )
            except (FileNotFoundError, configparser.Error, KeyError, ValueError):
                return False
        if self.platform == "win32":
            registry = self._windows_registry()
            try:
                with registry.OpenKey(registry.HKEY_CURRENT_USER, RUN_KEY) as key:
                    command, kind = registry.QueryValueEx(key, VALUE_NAME)
                if not command or kind != registry.REG_SZ:
                    return False
                return True
            except FileNotFoundError:
                return False
        return False

    def set_enabled(self, enabled: bool) -> None:
        if self.platform == "linux":
            if not enabled:
                self.entry.unlink(missing_ok=True)
                return
            command = self.command()
            content = (
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Version=1.0\n"
                "Name=talk2g\n"
                "Comment=Local progressive voice typing\n"
                "Exec=" + " ".join(desktop_argument(part) for part in command) + "\n"
                "Terminal=false\n"
                "StartupNotify=false\n"
                "X-GNOME-Autostart-enabled=true\n"
            )
            self.entry.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.entry.parent, prefix=".talk2g-", delete=False
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(content)
                temporary.replace(self.entry)
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
            return
        if self.platform == "win32":
            registry = self._windows_registry()
            if enabled:
                command = subprocess.list2cmdline(self.command())
                if len(command) > 260:
                    raise ValueError("Путь слишком длинный для автозапуска Windows. Переместите папку выше")
                with registry.CreateKeyEx(
                    registry.HKEY_CURRENT_USER, RUN_KEY, 0, registry.KEY_SET_VALUE
                ) as key:
                    registry.SetValueEx(key, VALUE_NAME, 0, registry.REG_SZ, command)
            else:
                try:
                    with registry.OpenKey(
                        registry.HKEY_CURRENT_USER, RUN_KEY, 0, registry.KEY_SET_VALUE
                    ) as key:
                        registry.DeleteValue(key, VALUE_NAME)
                except FileNotFoundError:
                    pass
            return
        raise RuntimeError("Автозапуск поддерживается в Linux и Windows")
