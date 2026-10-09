"""Protocol-v1 messages validated before they cross the transport boundary."""

import hmac
import json
import math
from dataclasses import replace

from .config import RATE, Settings

SESSION_FIELDS = ("save_recordings", "recognition_pause")

SERVER_FIELDS = {
    "ready": {},
    "commit": {"seq": int, "delta": str},
    "recognizing": {"audio_start": float, "audio_end": float, "capturing": bool},
    "segment_end": {},
    "session_end": {"text": str},
    "cancelled": {"text": str},
    "recording": {"status": str, "message": str},
    "error": {"message": str},
}


def read_server_event(message: str | bytes) -> dict:
    if not isinstance(message, str):
        raise ValueError("Некорректный ответ сервера: требуется текстовый JSON")
    try:
        event = json.loads(message)
    except json.JSONDecodeError as error:
        raise ValueError("Некорректный ответ сервера: неверный JSON") from error
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise ValueError("Некорректный ответ сервера: требуется объект с type")
    kind = event["type"]
    if kind not in SERVER_FIELDS:
        raise ValueError(f"Некорректный ответ сервера: неизвестное событие {kind}")
    fields = {
        "session_id": str,
        "model": str,
        "recording_path": str,
        "recording_error": str,
        "recognize_on_pause": bool,
        "recognition_pause": float,
        "audio_start": float,
        "audio_end": float,
        "audio_seconds": float,
        "elapsed_seconds": float,
        "decode_seconds": float,
        **SERVER_FIELDS[kind],
    }
    for name, expected in fields.items():
        if name not in event and name not in SERVER_FIELDS[kind]:
            continue
        value = event.get(name)
        valid = type(value) is expected
        if expected is float:
            valid = type(value) in (int, float) and value >= 0
            if type(value) is float:
                valid = valid and math.isfinite(value)
        if not valid:
            raise ValueError(f"Некорректный ответ сервера: поле {kind}.{name}")
    if kind == "commit" and (event["seq"] < 1 or not event["delta"]):
        raise ValueError("Некорректный ответ сервера: commit требует положительный seq и непустой delta")
    if kind == "recording" and event["status"] != "error":
        raise ValueError("Некорректный ответ сервера: recording.status должен быть error")
    if "recognition_pause" in event and not 0.5 <= event["recognition_pause"] <= 10:
        raise ValueError("Некорректный ответ сервера: recognition_pause вне диапазона 0.5–10")
    return event


def make_start(settings: Settings, session_id: str) -> dict:
    return {
        "type": "start",
        "version": 1,
        "format": "pcm16",
        "rate": RATE,
        "session_id": session_id,
        "token": settings.token,
        "recognize_on_pause": True,
        **{name: getattr(settings, name) for name in SESSION_FIELDS},
    }


def read_start(message: str | bytes, settings: Settings) -> dict:
    if not isinstance(message, str):
        raise ValueError("Первое сообщение должно быть start")
    start = json.loads(message)
    if (
        not isinstance(start, dict)
        or start.get("type") != "start"
        or type(start.get("version")) is not int
        or start.get("version") != 1
    ):
        raise ValueError("Неверная версия протокола или сообщение start")
    if settings.token and not hmac.compare_digest(
        str(start.get("token", "")).encode(), settings.token.encode()
    ):
        raise ValueError("Неверный токен сервера")
    if start.get("rate") != RATE or start.get("format") != "pcm16":
        raise ValueError("Требуется mono PCM16 16000 Hz")
    return start


def session_settings(defaults: Settings, start: dict) -> Settings:
    if start.get("recognize_on_pause", True) is not True:
        raise ValueError("Поддерживается только распознавание целых блоков после паузы")
    settings = replace(defaults, **{name: start[name] for name in SESSION_FIELDS if name in start})
    settings.validate()
    return settings
