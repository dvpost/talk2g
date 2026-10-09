from __future__ import annotations

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from pathlib import Path

from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response

from .config import RATE, Settings
from .model import GigaRecognizer, SileroDetector
from .protocol import read_start, session_settings
from .session import RecognitionSession
from .session_output import SessionOutput

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
        output = None
        session = None
        outcome = "disconnected"
        try:
            start = read_start(await asyncio.wait_for(socket.recv(), timeout=10), self.settings)
            if not self.recognizer:
                raise ValueError(
                    self.loading_error or "Модель ещё загружается. Повторите запуск через несколько секунд"
                )
            if self.active:
                raise ValueError("Сервер занят другой диктовкой")
            self.active = admitted = True
            settings = session_settings(self.settings, start)
            session = RecognitionSession(settings, self.detector_factory(), self.recognizer, self.pool)
            output = SessionOutput(settings, start.get("session_id", ""), socket.send, self.home)
            await output.ready(self.settings.model)
            worker = asyncio.create_task(session.run(output))
            while not session.stopped:
                receive = asyncio.create_task(socket.recv())
                done, _ = await asyncio.wait((receive, worker), return_when=asyncio.FIRST_COMPLETED)
                if worker in done:
                    receive.cancel()
                    await asyncio.gather(receive, return_exceptions=True)
                    await worker
                    return
                message = receive.result()
                if isinstance(message, bytes):
                    session.feed(message)
                    await output.append(message)
                else:
                    control = json.loads(message)
                    if not isinstance(control, dict):
                        raise ValueError("Неверное управляющее сообщение")
                    if control.get("type") == "stop":
                        session.stop()
                    elif control.get("type") == "cancel":
                        await output.send({"type": "cancelled", "text": session.text})
                        return
                    else:
                        raise ValueError("Неизвестное управляющее сообщение")
            await worker
        except ConnectionClosed:
            pass
        except Exception as error:
            outcome = "error"
            log.warning("Сессия завершена с ошибкой: %s", error)
            try:
                if output is not None:
                    await output.error(str(error), session.text)
                else:
                    await socket.send(
                        json.dumps({"type": "error", "message": str(error)}, ensure_ascii=False)
                    )
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
                if output is not None:
                    output.close(outcome, session.text)
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
