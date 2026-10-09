from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from urllib.error import URLError
from urllib.parse import urlsplit

import numpy as np
from PySide6.QtCore import QThread, Signal
from websockets.sync.client import connect

from .audio import SpeechTimeout, pcm, read_audio
from .config import RATE, Settings
from .model import SileroDetector, prepare_vad
from .protocol import make_start, read_server_event
from .service import health


class DictationThread(QThread):
    event = Signal(object)
    failure = Signal(str)
    level = Signal(float)
    connected = Signal()
    idle_remaining = Signal(float)
    idle_expired = Signal()
    pause_remaining = Signal(float)

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
        self._idle_control = (settings.stop_on_idle, 0.0)

    def set_idle_enabled(self, enabled: bool):
        self._idle_control = (enabled, time.monotonic())

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

        activity_packets: queue.Queue[tuple[bytes, float]] = queue.Queue(maxsize=600)

        def callback(data, frames, timing, status):
            if status:
                self.sender_error = f"Микрофон потерял аудио: {status}"
                self.stop()
            self.level.emit(float(np.sqrt(np.mean(data**2))))
            packet = pcm(data[:, 0])
            captured_at = time.monotonic()
            self._enqueue(packet)
            try:
                activity_packets.put_nowait((packet, captured_at))
            except queue.Full:
                self.sender_error = "Детектор речи не успевает обработать запись. Диктовка остановлена"
                self.stop()

        device = self.settings.microphone or None
        with sd.InputStream(
            device=device, samplerate=RATE, channels=1, dtype="float32", blocksize=1600, callback=callback
        ):
            self.capture_time = time.monotonic()
            self.connected.emit()
            self.capture_started.set()
            # Open the microphone before loading the small VAD. Run inference
            # here, outside PortAudio's real-time callback and the UI thread.
            timeout = self._speech_timeout()
            while not self.stop_requested.is_set():
                try:
                    packet, captured_at = activity_packets.get(timeout=0.05)
                    timeout.feed(packet, captured_at)
                except queue.Empty:
                    pass
                # Catch up with capture timestamps before deciding to stop.
                if activity_packets.empty():
                    self._check_idle_timeout(timeout)

    def _speech_timeout(self) -> SpeechTimeout:
        return SpeechTimeout(
            SileroDetector(prepare_vad()),
            self.settings.idle_timeout,
            self.capture_time,
            release_after=self.settings.recognition_pause,
        )

    def _check_idle_timeout(self, timeout: SpeechTimeout):
        enabled, enabled_at = self._idle_control
        if self.stop_requested.is_set():
            return
        now = time.monotonic()
        remaining = max(0, self.settings.recognition_pause - (now - timeout.last_speech))
        self.pause_remaining.emit(remaining if timeout.speech_seen else -1)
        if not enabled:
            return
        remaining = timeout.remaining(now, enabled_at)
        self.idle_remaining.emit(remaining)
        if remaining <= 0:
            self.stop()
            self.idle_expired.emit()

    def _capture_file(self):
        audio = read_audio(self.audio_file)
        self.capture_time = time.monotonic()
        self.connected.emit()
        self.capture_started.set()
        timeout = self._speech_timeout()
        began = time.monotonic()
        for i in range(0, len(audio), 1600):
            if self.stop_requested.is_set():
                break
            packet = pcm(audio[i : i + 1600])
            self._enqueue(packet)
            timeout.feed(packet, time.monotonic())
            self._check_idle_timeout(timeout)
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
        # EXC-0001: approved readiness retry; see docs/exceptional_execution_paths.md.
        while not self.cancel_requested.is_set():
            if self.sender_error:
                raise RuntimeError(self.sender_error)
            try:
                state = health(self.settings.server_url)
                if state.get("ready"):
                    return True
                if state.get("error"):
                    raise RuntimeError(state["error"])
            except URLError as error:
                address = urlsplit(self.settings.server_url)
                starting_local = (
                    self.settings.load_on_demand
                    and address.scheme == "ws"
                    and address.hostname in ("127.0.0.1", "localhost", "::1")
                )
                if not starting_local or not isinstance(error.reason, ConnectionRefusedError):
                    raise
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
                socket.send(json.dumps(make_start(self.settings, str(uuid.uuid4()))))
                response = read_server_event(socket.recv(timeout=15))
                if response.get("type") != "ready":
                    raise RuntimeError(response.get("message", "Сервер не готов"))
                if response.get("recognize_on_pause") is not True:
                    raise RuntimeError("Сервер не поддерживает распознавание после паузы. Обновите сервер")
                if self.settings.save_recordings:
                    if path := response.get("recording_path"):
                        self.event.emit({"type": "recording", "status": "started", "path": path})
                    elif response.get("recording_error"):
                        # EXC-0003: archive failure permits ASR; see docs/exceptional_execution_paths.md.
                        self.event.emit(
                            {
                                "type": "recording",
                                "status": "error",
                                "message": response["recording_error"],
                            }
                        )
                    else:
                        raise RuntimeError(
                            "Сервер не подтвердил сохранение записи. Перезапустите или обновите сервер"
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
                        response = read_server_event(socket.recv(timeout=0.5))
                    except TimeoutError:
                        if self.stop_time and time.monotonic() - self.stop_time > 45:
                            raise RuntimeError(
                                "Сервер не завершил запись за 45 секунд. Полученный текст сохранён"
                            ) from None
                        continue
                    if response.get("type") in ("session_end", "cancelled", "error"):
                        if path := response.get("recording_path"):
                            self.event.emit({"type": "recording", "status": "saved", "path": path})
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
