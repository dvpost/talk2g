from __future__ import annotations

import json
import queue
import threading
import time
import uuid

import numpy as np
from PySide6.QtCore import QThread, Signal
from websockets.sync.client import connect

from .audio import pcm, read_audio
from .config import RATE, Settings
from .service import health


class Delivery:
    """De-duplicate server events without pretending desktop insertion is transactional."""

    def __init__(self):
        self.sequence = 0
        self.text = ""

    def accept(self, event: dict) -> str:
        sequence = event["seq"]
        if type(sequence) is not int or sequence < 1:
            raise ValueError("Неверный номер фрагмента")
        if sequence <= self.sequence:
            return ""
        if sequence != self.sequence + 1:
            raise ValueError("Пропущен фрагмент. Диктовка остановлена, полученный текст сохранён")
        delta = event["delta"]
        if not isinstance(delta, str):
            raise ValueError("Неверный текст фрагмента")
        self.sequence = sequence
        self.text += delta
        return delta


class DictationThread(QThread):
    event = Signal(object)
    failure = Signal(str)
    level = Signal(float)
    connected = Signal()

    def __init__(
        self, settings: Settings, *, audio_file: str | None = None, activated_at: float | None = None
    ):
        super().__init__()
        self.settings = settings
        self.audio_file = audio_file
        self.stop_requested = threading.Event()
        self.cancel_requested = threading.Event()
        self.sender_error = ""
        self.stop_time = 0.0
        self.activated_at = activated_at or time.monotonic()
        self.capture_started = threading.Event()
        self.capture_finished = threading.Event()
        self.capture_time = 0.0
        self.captured_bytes = 0
        self.packets: queue.Queue[bytes] = queue.Queue(maxsize=600)
        self.startup_metrics = {}

    def stop(self):
        if not self.stop_requested.is_set():
            self.stop_time = time.monotonic()
            self.stop_requested.set()

    def cancel(self):
        self.cancel_requested.set()
        self.stop()

    def _enqueue(self, data: bytes):
        try:
            self.packets.put_nowait(data)
            self.captured_bytes += len(data)
        except queue.Full:
            self.sender_error = "Модель не успела принять 60 секунд записи. Диктовка остановлена"
            self.stop()

    def _capture_microphone(self):
        import sounddevice as sd

        def callback(data, frames, timing, status):
            if status:
                self.sender_error = f"Микрофон потерял аудио: {status}"
                self.stop()
            self.level.emit(float(np.sqrt(np.mean(data**2))))
            self._enqueue(pcm(data[:, 0]))

        device = self.settings.microphone or None
        with sd.InputStream(
            device=device, samplerate=RATE, channels=1, dtype="float32", blocksize=1600, callback=callback
        ):
            self.capture_time = time.monotonic()
            self.connected.emit()
            self.capture_started.set()
            self.stop_requested.wait()

    def _capture_file(self):
        audio = read_audio(self.audio_file)
        self.capture_time = time.monotonic()
        self.connected.emit()
        self.capture_started.set()
        began = time.monotonic()
        for i in range(0, len(audio), 1600):
            if self.stop_requested.is_set():
                break
            self._enqueue(pcm(audio[i : i + 1600]))
            end = min(i + 1600, len(audio))
            self.stop_requested.wait(max(0, began + end / RATE - time.monotonic()))

    def _capture(self):
        try:
            if self.audio_file:
                self._capture_file()
            else:
                self._capture_microphone()
        except Exception as error:
            self.sender_error = str(error)
        finally:
            self.stop()
            self.capture_started.set()
            self.capture_finished.set()

    def _wait_for_server(self) -> bool:
        began = time.monotonic()
        reported = -1
        while not self.cancel_requested.is_set():
            if self.sender_error:
                raise RuntimeError(self.sender_error)
            try:
                state = health(self.settings.server_url)
                if state.get("ready"):
                    return True
                if state.get("error"):
                    raise RuntimeError(state["error"])
            except OSError:
                pass  # The on-demand subprocess may not have bound its port yet.
            elapsed = time.monotonic() - began
            if elapsed > 60:
                raise RuntimeError("Модель не подготовилась за 60 секунд. Проверьте .data/server.log")
            if int(elapsed) != reported:
                reported = int(elapsed)
                self.event.emit(
                    {
                        "type": "loading",
                        "seconds": elapsed,
                        "buffered_audio_seconds": self.captured_bytes / (2 * RATE),
                        "capturing": not self.stop_requested.is_set(),
                    }
                )
            self.cancel_requested.wait(0.05)
        return False

    def _send_audio(self, socket):
        # Drain pre-load audio as fast as the connection accepts it; never wait
        # for real-time playback of an already recorded packet.
        while not self.cancel_requested.is_set():
            if self.capture_finished.is_set() and self.packets.empty():
                break
            try:
                socket.send(self.packets.get(timeout=0.05))
            except queue.Empty:
                pass
        socket.send(json.dumps({"type": "cancel" if self.cancel_requested.is_set() else "stop"}))

    def run(self):
        sender = None
        capture = threading.Thread(target=self._capture, name="microphone-capture", daemon=True)
        try:
            capture.start()
            if not self.capture_started.wait(8):
                raise RuntimeError("Микрофон не открылся за 8 секунд")
            if self.sender_error:
                raise RuntimeError(self.sender_error)
            if not self._wait_for_server():
                return
            with connect(
                self.settings.server_url,
                compression=None,
                max_size=1_000_000,
                open_timeout=8,
                close_timeout=3,
                proxy=None,
            ) as socket:
                socket.send(
                    json.dumps(
                        {
                            "type": "start",
                            "version": 1,
                            "format": "pcm16",
                            "rate": RATE,
                            "session_id": str(uuid.uuid4()),
                            "token": self.settings.token,
                            "interval": self.settings.interval,
                            "holdback": self.settings.holdback,
                            "silence": self.settings.silence,
                            "window": self.settings.window,
                            "dual_window": self.settings.dual_window,
                            "fast_window": self.settings.fast_window,
                            "quality_window": self.settings.quality_window,
                            "quality_interval": self.settings.quality_interval,
                            "quality_holdback": self.settings.quality_holdback,
                            "lm_rescore": self.settings.dual_window and self.settings.lm_rescore,
                            "lm_margin": self.settings.lm_margin,
                        }
                    )
                )
                response = json.loads(socket.recv(timeout=15))
                if response.get("type") != "ready":
                    raise RuntimeError(response.get("message", "Сервер не готов"))
                if self.settings.dual_window and response.get("dual_window") is not True:
                    raise RuntimeError("Сервер не поддерживает два окна. Обновите сервер или снимите галочку")
                if (
                    self.settings.dual_window
                    and self.settings.lm_rescore
                    and response.get("lm_rescore") is not True
                ):
                    raise RuntimeError(
                        "Сервер не поддерживает ruGPT. Обновите сервер или снимите галочку сравнения"
                    )
                self.startup_metrics = {
                    "type": "model_ready",
                    "load_wait_seconds": time.monotonic() - self.capture_time,
                    "activation_seconds": time.monotonic() - self.activated_at,
                    "buffered_audio_seconds": self.captured_bytes / (2 * RATE),
                    "capturing": not self.stop_requested.is_set(),
                }
                self.event.emit(self.startup_metrics)
                if self.stop_requested.is_set():
                    self.stop_time = time.monotonic()  # finalization timeout excludes model loading

                def sending():
                    try:
                        self._send_audio(socket)
                    except Exception as error:
                        self.sender_error = str(error)
                        self.stop()
                        socket.close()

                sender = threading.Thread(target=sending, name="microphone-sender", daemon=True)
                sender.start()
                while True:
                    try:
                        response = json.loads(socket.recv(timeout=0.5))
                    except TimeoutError:
                        if self.stop_time and time.monotonic() - self.stop_time > 45:
                            raise RuntimeError(
                                "Сервер не завершил запись за 45 секунд. Полученный текст сохранён"
                            ) from None
                        continue
                    if response.get("type") == "error":
                        raise RuntimeError(response["message"])
                    self.event.emit(response)
                    if response.get("type") in ("session_end", "cancelled"):
                        break
                if self.sender_error:
                    raise RuntimeError(self.sender_error)
        except Exception as error:
            self.failure.emit(self.sender_error or str(error))
        finally:
            self.stop_requested.set()
            if capture.is_alive():
                capture.join(timeout=5)
            if sender:
                sender.join(timeout=5)
