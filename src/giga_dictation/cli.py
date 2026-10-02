from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .audio import pcm, read_audio
from .config import RATE, Settings, project_home
from .model import GigaRecognizer, Word, prepare_models
from .transcript import Transcript


async def replay(
    path: str, url: str, token: str = "", *, speed: float = 1.0, events_path: str | None = None
) -> dict:
    from websockets.asyncio.client import connect

    audio = read_audio(path)
    events = []
    began = time.monotonic()
    async with connect(url, compression=None, max_size=1_000_000, proxy=None) as socket:
        await socket.send(
            json.dumps(
                {
                    "type": "start",
                    "version": 1,
                    "rate": RATE,
                    "format": "pcm16",
                    "session_id": str(uuid.uuid4()),
                    "token": token,
                }
            )
        )
        ready = json.loads(await socket.recv())
        if ready.get("type") != "ready":
            raise RuntimeError(ready.get("message", "Сервер не готов"))

        async def sender():
            for offset in range(0, len(audio), 1600):
                await socket.send(pcm(audio[offset : offset + 1600]))
                if speed > 0:
                    deadline = began + min(offset + 1600, len(audio)) / RATE / speed
                    await asyncio.sleep(max(0, deadline - time.monotonic()))
            await socket.send('{"type":"stop"}')

        sending = asyncio.create_task(sender())
        try:
            async for payload in socket:
                event = json.loads(payload)
                event["received_seconds"] = time.monotonic() - began
                events.append(event)
                if event["type"] == "commit":
                    print(event["delta"], end="", flush=True)
                if event["type"] == "error":
                    raise RuntimeError(event["message"])
                if event["type"] == "session_end":
                    print()
                    return event
            raise RuntimeError("Сервер закрыл соединение до завершения записи")
        finally:
            sending.cancel()
            await asyncio.gather(sending, return_exceptions=True)
            if events_path:
                Path(events_path).write_text(
                    json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8"
                )


def transcribe(path: str, settings: Settings) -> str:
    audio = read_audio(path)
    recognizer = GigaRecognizer(settings)
    transcript = Transcript(settings.holdback)
    step = int(10 * RATE)
    overlap = int(max(1.5, settings.holdback + 0.5) * RATE)
    for start in range(0, len(audio), step - overlap):
        window = audio[start : start + step]
        words = [
            Word(w.text, w.start + start / RATE, w.end + start / RATE) for w in recognizer.decode(window)
        ]
        is_last = start + step >= len(audio)
        transcript.update(words, (start + len(window)) / RATE, final=True, forced=not is_last)
        if is_last:
            break
    return transcript.text


def doctor() -> dict:
    import shutil

    import onnxruntime

    result = {
        "python": sys.version.split()[0],
        "home": str(project_home()),
        "providers": onnxruntime.get_available_providers(),
        "cpu_count": os.cpu_count(),
        "desktop": os.environ.get("XDG_SESSION_TYPE", os.name),
    }
    try:
        import sounddevice as sd

        result["microphones"] = [
            {"index": i, "name": d["name"], "rate": d["default_samplerate"]}
            for i, d in enumerate(sd.query_devices())
            if d["max_input_channels"] > 0
        ]
        sd.check_input_settings(samplerate=RATE, channels=1)
        result["microphone_16khz"] = True
    except Exception as error:
        result["microphone_error"] = str(error)
    for program in ("xdotool", "wl-copy"):
        result[program] = shutil.which(program)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Giga Dictation — локальная потоковая диктовка")
    sub = parser.add_subparsers(dest="command")
    app_parser = sub.add_parser("app", help="Приложение, локальный сервер запускается автоматически")
    app_parser.add_argument("--background", action="store_true", help="Запустить в системном трее")
    app_parser.add_argument("--home", type=Path, help="Каталог настроек и моделей")
    sub.add_parser("toggle", help="Начать/остановить диктовку в уже запущенном приложении")
    sub.add_parser("quit", help="Завершить уже запущенное приложение и его локальный сервер")
    sub.add_parser("doctor", help="Проверить зависимости и микрофон")
    download = sub.add_parser("download", help="Скачать модели один раз")
    download.add_argument("--model", choices=["gigaam-v3-e2e-ctc", "gigaam-v3-e2e-rnnt"])
    download.add_argument(
        "--language-model", action="store_true", help="Также скачать ruGPT3-small INT8 (280 МБ)"
    )
    server = sub.add_parser("server", help="Фоновый сервер GigaAM")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", default=8769, type=int)
    server.add_argument("--threads", type=int)
    server.add_argument("--model", choices=["gigaam-v3-e2e-ctc", "gigaam-v3-e2e-rnnt"])
    file_parser = sub.add_parser("transcribe", help="Распознать аудиофайл")
    file_parser.add_argument("audio")
    file_parser.add_argument("--model", choices=["gigaam-v3-e2e-ctc", "gigaam-v3-e2e-rnnt"])
    replay_parser = sub.add_parser("replay", help="Проверить потоковый сервер настоящим аудио")
    replay_parser.add_argument("audio")
    replay_parser.add_argument("--url")
    replay_parser.add_argument("--speed", type=float, default=1)
    replay_parser.add_argument("--events", help="Сохранить события и задержки в JSON")
    args = parser.parse_args()
    if getattr(args, "home", None):
        os.environ["GIGA_DICTATION_HOME"] = str(args.home.expanduser().resolve())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for name in ("httpx", "httpcore", "huggingface_hub", "websockets.server"):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        settings = Settings.load()
        settings.token = os.environ.get("GIGA_DICTATION_TOKEN", settings.token)
        if getattr(args, "model", None):
            settings.model = args.model
        if getattr(args, "threads", None):
            settings.threads = args.threads
        settings.validate()
        if args.command == "doctor":
            print(json.dumps(doctor(), ensure_ascii=False, indent=2))
        elif args.command == "download":
            prepare_models(settings)
            if args.language_model:
                from .language_model import prepare_language_model

                prepare_language_model()
            print("Модели скачаны. Дальнейшая локальная диктовка работает без интернета.")
        elif args.command == "server":
            from .server import DictationServer

            asyncio.run(DictationServer(settings).run(args.host, args.port))
        elif args.command == "transcribe":
            print(transcribe(args.audio, settings))
        elif args.command == "replay":
            result = asyncio.run(
                replay(
                    args.audio,
                    args.url or settings.server_url,
                    settings.token,
                    speed=args.speed,
                    events_path=args.events,
                )
            )
            print(json.dumps(result, ensure_ascii=False))
        elif args.command in ("toggle", "quit"):
            from .runtime import desktop_runtime

            desktop_runtime()
            from .desktop import send_control

            if not send_control(args.command):
                raise RuntimeError("Сначала запустите Giga Dictation")
        else:
            from .runtime import desktop_runtime

            desktop_runtime()
            log_path = project_home() / ".data/app.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
            log_path.chmod(0o600)
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logging.getLogger().addHandler(handler)
            from .desktop import run_app

            run_app(settings, background=getattr(args, "background", False))
    except KeyboardInterrupt:
        pass
    except Exception as error:
        logging.error("%s", error)
        raise SystemExit(1) from error
