from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path

from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response

from .audio import Segmenter
from .config import RATE, Settings
from .model import GigaRecognizer, SileroDetector, Word
from .transcript import Transcript

log = logging.getLogger(__name__)


class DictationServer:
    def __init__(
        self,
        settings: Settings,
        *,
        recognizer=None,
        detector_factory=None,
        home: Path | None = None,
    ):
        self.settings = settings
        self.home = home
        self.recognizer = recognizer
        self.detector_factory = detector_factory
        self.active = False
        self.loading_error = ""
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="talk2g-asr")

    async def load(self) -> None:
        if self.recognizer is not None:
            return
        try:
            self.recognizer = await asyncio.get_running_loop().run_in_executor(
                self.pool,
                GigaRecognizer,
                self.settings,
                self.home,
            )
            self.detector_factory = lambda: SileroDetector(self.recognizer.vad_path)
        except Exception as error:
            self.loading_error = str(error)
            log.exception("Не удалось загрузить GigaAM")

    async def health(self, connection, request):
        if request.path == "/health":
            body = json.dumps(
                {
                    "app": "talk2g",
                    "protocol": 1,
                    "ready": self.recognizer is not None,
                    "model": self.settings.model,
                    "busy": self.active,
                    "error": self.loading_error,
                }
            ).encode()
            return Response(HTTPStatus.OK, "OK", Headers({"Content-Type": "application/json"}), body)
        if request.path != "/v1/dictate":
            return Response(HTTPStatus.NOT_FOUND, "Not Found", Headers(), b"Unknown route")
        # A native client has no Origin. Never accept drive-by browser microphone sessions.
        if request.headers.get("Origin"):
            return Response(HTTPStatus.FORBIDDEN, "Forbidden", Headers(), b"Native clients only")
        return None

    async def handle(self, socket: ServerConnection) -> None:
        admitted = False
        worker = None
        receive = None
        try:
            message = await asyncio.wait_for(socket.recv(), timeout=10)
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
            if self.settings.token and not hmac.compare_digest(
                str(start.get("token", "")).encode(), self.settings.token.encode()
            ):
                raise ValueError("Неверный токен сервера")
            if start.get("rate") != RATE or start.get("format") != "pcm16":
                raise ValueError("Требуется mono PCM16 16000 Hz")
            if not self.recognizer:
                raise ValueError(
                    self.loading_error or "Модель ещё загружается. Повторите запуск через несколько секунд"
                )
            if self.active:
                raise ValueError("Сервер занят другой диктовкой")
            self.active = admitted = True
            settings = replace(self.settings)
            for name in ("interval", "holdback", "silence", "window"):
                if name in start:
                    setattr(settings, name, start[name])
            settings.validate()
            segmenter = Segmenter(self.detector_factory(), settings)
            transcript = Transcript(settings.holdback)
            changed = asyncio.Event()
            stopped = False
            sent_bytes = 0
            started_at = time.monotonic()

            async def send(event):
                event["session_id"] = start.get("session_id", "")
                await socket.send(json.dumps(event, ensure_ascii=False))

            async def process():
                last_decode_at = 0.0
                while True:
                    delay = max(0.0, settings.interval - (time.monotonic() - last_decode_at))
                    if segmenter.finished or stopped:
                        delay = 0
                    if delay:
                        try:
                            await asyncio.wait_for(changed.wait(), delay)
                            changed.clear()
                        except TimeoutError:
                            pass
                        if (
                            not segmenter.finished
                            and not stopped
                            and time.monotonic() - last_decode_at < settings.interval
                        ):
                            continue
                    segment = segmenter.finished.popleft() if segmenter.finished else segmenter.current
                    if segment is None or (
                        not segment.closed and (segment.count < RATE or segment.end == segment.decoded_end)
                    ):
                        if stopped and not segmenter.finished:
                            await send(
                                {
                                    "type": "session_end",
                                    "text": transcript.text,
                                    "audio_seconds": sent_bytes / (2 * RATE),
                                    "elapsed_seconds": time.monotonic() - started_at,
                                }
                            )
                            return
                        changed.clear()
                        await changed.wait()
                        continue
                    audio, offset = segment.snapshot()
                    snapshot_end = offset + len(audio) / RATE
                    decode_start = time.monotonic()
                    words = await asyncio.get_running_loop().run_in_executor(
                        self.pool, self.recognizer.decode, audio
                    )
                    words = [Word(w.text, w.start + offset, w.end + offset) for w in words]
                    segment.decoded_end = round(snapshot_end * RATE)
                    # A segment can close while native inference is running. Only finalize the
                    # exact snapshot that includes its tail; queued final decode handles the rest.
                    final = segment.closed and segment.decoded_end == segment.end
                    if final and segment in segmenter.finished:
                        segmenter.finished.remove(segment)
                    delta, partial = transcript.update(
                        words, snapshot_end, final=final, forced=segment.forced
                    )
                    duration = time.monotonic() - decode_start
                    if delta:
                        await send(
                            {
                                "type": "commit",
                                "seq": transcript.sequence,
                                "delta": delta,
                                "audio_end": transcript.frontier,
                                "decode_seconds": duration,
                            }
                        )
                    await send(
                        {
                            "type": "partial",
                            "text": partial,
                            "decode_seconds": duration,
                            "buffer_seconds": segmenter.buffered_seconds,
                        }
                    )
                    if final and not segment.forced:
                        await send({"type": "segment_end"})
                    if segment is segmenter.current and transcript.frontier > 0:
                        segment.trim(int((transcript.frontier - 1.2) * RATE))
                    last_decode_at = time.monotonic()

            await send({"type": "ready", "model": self.settings.model})
            worker = asyncio.create_task(process())
            while not stopped:
                receive = asyncio.create_task(socket.recv())
                done, _ = await asyncio.wait((receive, worker), return_when=asyncio.FIRST_COMPLETED)
                if worker in done:
                    receive.cancel()
                    await asyncio.gather(receive, return_exceptions=True)
                    await worker
                    return
                message = receive.result()
                if isinstance(message, bytes):
                    if len(message) > 2 * RATE:
                        raise ValueError("Аудиопакет не должен превышать одну секунду")
                    sent_bytes += len(message)
                    if sent_bytes > 2 * RATE * 7200:
                        raise ValueError("Максимальная продолжительность сессии — два часа")
                    segmenter.feed(message)
                    changed.set()
                else:
                    control = json.loads(message)
                    if not isinstance(control, dict):
                        raise ValueError("Неверное управляющее сообщение")
                    if control.get("type") == "stop":
                        segmenter.stop()
                        stopped = True
                        changed.set()
                    elif control.get("type") == "cancel":
                        await send({"type": "cancelled", "text": transcript.text})
                        return
                    else:
                        raise ValueError("Неизвестное управляющее сообщение")
            await worker
        except ConnectionClosed:
            pass
        except Exception as error:
            log.warning("Сессия завершена с ошибкой: %s", error)
            try:
                await socket.send(json.dumps({"type": "error", "message": str(error)}, ensure_ascii=False))
            except ConnectionClosed:
                pass
        finally:
            if receive and not receive.done():
                receive.cancel()
                await asyncio.gather(receive, return_exceptions=True)
            if worker:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            if admitted:
                self.active = False

    async def run(self, host: str = "127.0.0.1", port: int = 8769):
        self.settings.validate()
        if host not in ("127.0.0.1", "localhost", "::1") and not self.settings.token:
            raise ValueError("Для сетевого сервера задайте TALK2G_TOKEN")
        try:
            async with serve(
                self.handle,
                host,
                port,
                process_request=self.health,
                compression=None,
                max_size=2 * RATE,
                max_queue=16,
                ping_timeout=60,
            ):
                log.info("Сервер слушает %s:%d", host, port)
                await self.load()
                if self.loading_error:
                    raise RuntimeError(self.loading_error)
                await asyncio.Future()
        finally:
            self.pool.shutdown(wait=True, cancel_futures=True)
