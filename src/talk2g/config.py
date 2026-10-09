from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

RATE = 16_000


def project_home() -> Path:
    if value := os.environ.get("TALK2G_HOME"):
        return Path(value).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


@dataclass
class Settings:
    server_url: str = "ws://127.0.0.1:8769/v1/dictate"
    token: str = ""
    model: str = "gigaam-v3-e2e-ctc"
    threads: int = 4
    auto_insert: bool = True
    microphone: str = ""
    hotkey: str = "<ctrl>+<shift>+a"
    paste_mode: str = "auto"
    copy_on_stop: bool = True
    show_overlay: bool = True
    overlay_position: str = "top_right"
    load_on_demand: bool = False
    autostart: bool = False
    stop_on_phrase: bool = False
    stop_on_idle: bool = True
    idle_timeout: int = 45
    save_recordings: bool = False
    recognition_pause: float = 3.0

    def validate(self) -> None:
        for name in (
            "server_url",
            "token",
            "model",
            "microphone",
            "hotkey",
            "paste_mode",
            "overlay_position",
        ):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name}: требуется строка")
        for name in (
            "auto_insert",
            "copy_on_stop",
            "show_overlay",
            "load_on_demand",
            "autostart",
            "stop_on_phrase",
            "stop_on_idle",
            "save_recordings",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name}: требуется true или false")
        if self.paste_mode not in ("auto", "ctrl_v", "ctrl_shift_v", "shift_insert"):
            raise ValueError("Неизвестный способ вставки")
        if self.overlay_position not in (
            "top_right",
            "middle_right",
            "bottom_right",
            "top_left",
            "middle_left",
            "bottom_left",
        ):
            raise ValueError("Неизвестное положение окна диктовки")
        ranges = {
            "recognition_pause": (0.5, 10),
        }
        for key, (low, high) in ranges.items():
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{key}: требуется число")
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{key}: допустимо от {low} до {high}")
        if type(self.threads) is not int or not 1 <= self.threads <= 32:
            raise ValueError("threads: допустимо от 1 до 32")
        if type(self.idle_timeout) is not int or not 1 <= self.idle_timeout <= 7200:
            raise ValueError("idle_timeout: допустимо от 1 до 7200 секунд")
        if self.model not in ("gigaam-v3-e2e-ctc", "gigaam-v3-e2e-rnnt"):
            raise ValueError("Неизвестная модель GigaAM")
        if not isinstance(self.server_url, str) or not self.server_url.startswith(("ws://", "wss://")):
            raise ValueError("Адрес сервера должен начинаться с ws:// или wss://")
        address = urlsplit(self.server_url)
        if not address.hostname or address.path != "/v1/dictate" or address.username or address.query:
            raise ValueError("Адрес сервера: ws://host:port/v1/dictate")
        if address.port is not None and not 1 <= address.port <= 65535:
            raise ValueError("Некорректный порт сервера")

    @classmethod
    def load(cls, home: Path | None = None) -> Settings:
        path = (home or project_home()) / ".data" / "settings.json"
        if not path.exists():
            return cls()
        values = json.loads(path.read_text(encoding="utf-8"))
        allowed = cls.__dataclass_fields__
        settings = cls(**{key: value for key, value in values.items() if key in allowed})
        settings.validate()
        return settings

    def save(self, home: Path | None = None) -> None:
        self.validate()
        path = (home or project_home()) / ".data" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
