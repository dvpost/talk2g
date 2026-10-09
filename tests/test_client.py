import asyncio
import json
import queue
import sys
import threading
import time
import wave
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import numpy as np
import pytest
from PySide6.QtCore import Qt
from websockets.asyncio.server import serve

from talk2g import client
from talk2g.audio import pcm
from talk2g.config import RATE, Settings
from talk2g.server import DictationServer


@pytest.fixture(autouse=True)
def fake_vad(monkeypatch):
    class Detector:
        def probability(self, frame):
            return float(np.max(np.abs(frame)) > 0.05)

    monkeypatch.setattr(client, "prepare_vad", lambda: None)
    monkeypatch.setattr(client, "SileroDetector", lambda path: Detector())


@pytest.fixture
def capture_transport(monkeypatch):
    """Leave capture real; replace the unrelated server at its public boundary."""
    failures = []

    class Socket:
        def __enter__(self):
            self.ready_sent = False
            return self

        def __exit__(self, *args):
            pass

        def send(self, packet):
            pass

        def recv(self, **kwargs):
            if not self.ready_sent:
                self.ready_sent = True
                return '{"type":"ready","recognize_on_pause":true}'
            if not worker.capture_finished.wait(4):
                raise TimeoutError
            return '{"type":"session_end","text":""}'

        def close(self):
            pass

    monkeypatch.setattr(client, "connect", lambda *args, **kwargs: Socket())
    monkeypatch.setattr(client, "health", lambda url: {"ready": True})

    def run(capture_worker):
        nonlocal worker
        worker = capture_worker
        worker.failure.connect(failures.append, Qt.ConnectionType.DirectConnection)
        worker.run()
        assert failures == []

    worker = None
    return run


@pytest.fixture
def capture_clock(monkeypatch):
    """Advance capture pacing through Event.wait without changing application internals."""
    now, steps = [100.0], deque()

    class PacedEvent(threading.Event):
        def wait(self, timeout=None):
            if self is worker.stop_requested and threading.current_thread().name == "microphone-capture":
                if steps:
                    now[0], enabled = steps.popleft()
                    if enabled is not None:
                        worker.set_idle_enabled(enabled)
                return super().wait(0)
            return super().wait(timeout)

    monkeypatch.setattr(client, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(client, "threading", SimpleNamespace(Event=PacedEvent, Thread=threading.Thread))

    def schedule(capture_worker, changes):
        nonlocal worker
        worker = capture_worker
        steps.extend(changes)

    worker = None
    return schedule


@pytest.mark.parametrize("duration,load_delay", [(0.4, 0.8), (1.0, 0.4)])
async def test_loading_buffers_every_sample_and_sends_backlog_without_replaying(
    monkeypatch, duration, load_delay
):
    audio = np.linspace(-0.4, 0.4, int(duration * RATE), dtype=np.float32)
    monkeypatch.setattr(client, "read_audio", lambda path: audio)
    began = time.monotonic()
    monkeypatch.setattr(client, "health", lambda url: {"ready": time.monotonic() - began >= load_delay})
    received = []
    first_packet_at = last_packet_at = None

    async def handle(socket):
        nonlocal first_packet_at, last_packet_at
        assert json.loads(await socket.recv())["type"] == "start"
        await socket.send('{"type":"ready","recognize_on_pause":true}')
        async for packet in socket:
            if isinstance(packet, bytes):
                first_packet_at = first_packet_at or time.monotonic()
                last_packet_at = time.monotonic()
                received.append(packet)
            else:
                assert json.loads(packet)["type"] == "stop"
                await socket.send('{"type":"session_end","text":""}')
                return

    async with serve(handle, "127.0.0.1", 0) as listener:
        port = listener.sockets[0].getsockname()[1]
        worker = client.DictationThread(
            Settings(server_url=f"ws://127.0.0.1:{port}/v1/dictate", load_on_demand=True), audio_file="test"
        )
        events, failures = [], []
        worker.event.connect(events.append, Qt.ConnectionType.DirectConnection)
        worker.failure.connect(failures.append, Qt.ConnectionType.DirectConnection)
        await asyncio.wait_for(asyncio.to_thread(worker.run), 4)
    assert not failures
    assert b"".join(received) == pcm(audio)  # includes the beginning captured before readiness
    assert worker.capture_finished.is_set() and worker.packets.empty()
    assert any(event["type"] == "loading" for event in events)
    assert worker.startup_metrics["buffered_audio_seconds"] >= min(duration, load_delay)
    assert last_packet_at - first_packet_at < max(0.2, duration - load_delay + 0.15)
    assert events[-1]["type"] == "session_end"


async def test_stop_during_loading_closes_capture_but_still_recognizes_buffer(monkeypatch):
    audio = np.full(2 * RATE, 0.2, dtype=np.float32)
    monkeypatch.setattr(client, "read_audio", lambda path: audio)
    ready = threading.Event()
    monkeypatch.setattr(client, "health", lambda url: {"ready": ready.is_set()})
    received = bytearray()

    async def handle(socket):
        await socket.recv()
        await socket.send('{"type":"ready","recognize_on_pause":true}')
        async for packet in socket:
            if isinstance(packet, bytes):
                received.extend(packet)
            else:
                assert json.loads(packet)["type"] == "stop"
                await socket.send('{"type":"session_end","text":""}')
                return

    async with serve(handle, "127.0.0.1", 0) as listener:
        port = listener.sockets[0].getsockname()[1]
        worker = client.DictationThread(
            Settings(server_url=f"ws://127.0.0.1:{port}/v1/dictate"), audio_file="test"
        )
        failures = []
        worker.failure.connect(failures.append, Qt.ConnectionType.DirectConnection)
        task = asyncio.create_task(asyncio.to_thread(worker.run))
        assert await asyncio.to_thread(worker.capture_started.wait, 1)
        await asyncio.sleep(0.22)
        worker.stop()
        assert await asyncio.to_thread(worker.capture_finished.wait, 1)
        bytes_at_stop = worker.captured_bytes
        assert 0 < bytes_at_stop < len(pcm(audio)) and not task.done()
        ready.set()
        await asyncio.wait_for(task, 2)
    assert not failures and len(received) == bytes_at_stop
    assert bytes(received) == pcm(audio)[:bytes_at_stop]
    assert not worker.startup_metrics["capturing"]


async def test_cancel_during_loading_stops_capture_without_waiting_for_model(monkeypatch):
    monkeypatch.setattr(client, "read_audio", lambda path: np.zeros(10 * RATE))
    monkeypatch.setattr(client, "health", lambda url: {"ready": False})
    worker = client.DictationThread(Settings(load_on_demand=True), audio_file="test")
    failures = []
    worker.failure.connect(failures.append, Qt.ConnectionType.DirectConnection)
    task = asyncio.create_task(asyncio.to_thread(worker.run))
    assert await asyncio.to_thread(worker.capture_started.wait, 1)
    worker.cancel()
    await asyncio.wait_for(task, 1)
    assert worker.capture_finished.is_set() and not failures
    assert not worker.startup_metrics


async def test_full_buffer_stops_with_explicit_error_instead_of_dropping_words(monkeypatch):
    monkeypatch.setattr(client, "read_audio", lambda path: np.zeros(RATE))
    monkeypatch.setattr(client, "health", lambda url: {"ready": False})
    worker = client.DictationThread(Settings(load_on_demand=True), audio_file="test")
    worker.packets = queue.Queue(maxsize=2)
    failures = []
    worker.failure.connect(failures.append, Qt.ConnectionType.DirectConnection)
    await asyncio.wait_for(asyncio.to_thread(worker.run), 2)
    assert worker.capture_finished.is_set()
    assert failures and "60 секунд" in failures[0]
    assert worker.packets.qsize() == 2


@pytest.mark.parametrize("load_delay", [0, 1.4])
async def test_idle_timeout_closes_capture_and_finalizes_all_buffered_audio(monkeypatch, load_delay):
    monkeypatch.setattr(client, "read_audio", lambda path: np.zeros(3 * RATE))
    began = time.monotonic()
    monkeypatch.setattr(client, "health", lambda url: {"ready": time.monotonic() - began >= load_delay})
    received = bytearray()

    async def handle(socket):
        await socket.recv()
        await socket.send('{"type":"ready","recognize_on_pause":true}')
        async for packet in socket:
            if isinstance(packet, bytes):
                received.extend(packet)
            else:
                assert json.loads(packet)["type"] == "stop"
                await socket.send(json.dumps({"type": "commit", "seq": 1, "delta": "Хвост записи."}))
                await socket.send(json.dumps({"type": "session_end", "text": "Хвост записи."}))
                return

    async with serve(handle, "127.0.0.1", 0) as listener:
        port = listener.sockets[0].getsockname()[1]
        worker = client.DictationThread(
            Settings(server_url=f"ws://127.0.0.1:{port}/v1/dictate", idle_timeout=1), audio_file="test"
        )
        events, failures, countdown, expired = [], [], [], []
        worker.event.connect(events.append, Qt.ConnectionType.DirectConnection)
        worker.failure.connect(failures.append, Qt.ConnectionType.DirectConnection)
        worker.idle_remaining.connect(countdown.append, Qt.ConnectionType.DirectConnection)
        worker.idle_expired.connect(lambda: expired.append(True), Qt.ConnectionType.DirectConnection)
        await asyncio.wait_for(asyncio.to_thread(worker.run), 4)
    assert not failures and expired == [True]
    assert worker.capture_finished.is_set() and worker.packets.empty()
    assert len(received) == worker.captured_bytes
    assert 0.9 <= len(received) / (2 * RATE) <= 1.3
    assert countdown[0] > 0.8 and countdown[-1] == 0
    assert events[-1] == {"type": "session_end", "text": "Хвост записи."}


async def test_new_speech_resets_timeout_and_long_speech_keeps_capture_running(
    monkeypatch, capture_transport
):
    # GIVEN: начальная тишина, речь дольше тайм-аута и завершающая пауза.
    audio = np.concatenate([np.zeros(6400), np.full(19200, 0.2), np.zeros(22400)])
    monkeypatch.setattr(client, "read_audio", lambda path: audio)
    worker = client.DictationThread(Settings(idle_timeout=1), audio_file="test")
    countdown, expired = [], []
    worker.idle_remaining.connect(countdown.append, Qt.ConnectionType.DirectConnection)
    worker.idle_expired.connect(lambda: expired.append(True), Qt.ConnectionType.DirectConnection)
    # WHEN: запускаем диктовку через поддерживаемую входную точку.
    await asyncio.wait_for(asyncio.to_thread(capture_transport, worker), 4)
    # THEN: речь сбрасывает отсчёт; запись завершается только после полной паузы.
    assert worker.capture_finished.is_set() and expired == [True]
    assert 2.5 <= worker.captured_bytes / (2 * RATE) <= 2.9
    assert min(countdown[:4]) < 0.8
    assert min(countdown[5:16]) > 0.9  # speech longer than the timeout keeps resetting it
    assert countdown[-1] == 0


@pytest.mark.parametrize(
    "speech_packets,vad_delay", [(0, 0), (14, 1.2)], ids=["initial-silence", "speech-during-vad-loading"]
)
async def test_microphone_timeout_handles_initial_silence_and_delayed_vad(
    monkeypatch, capture_transport, speech_packets, vad_delay
):
    # GIVEN: микрофон передаёт тишину либо речь; подготовка VAD может задерживаться.
    class Stream:
        def __init__(self, *, callback, **kwargs):
            self.callback = callback
            self.done = threading.Event()
            self.producer = threading.Thread(target=self.produce)

        def produce(self):
            index = 0
            while not self.done.is_set():
                value = 0.2 if index < speech_packets else 0
                self.callback(np.full((1600, 1), value, dtype=np.float32), 1600, None, False)
                index += 1
                self.done.wait(0.1)

        def __enter__(self):
            self.producer.start()
            return self

        def __exit__(self, *args):
            self.done.set()
            self.producer.join(timeout=1)

    def prepare_vad():
        time.sleep(vad_delay)

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(InputStream=Stream))
    monkeypatch.setattr(client, "prepare_vad", prepare_vad)
    worker = client.DictationThread(Settings(idle_timeout=1))
    expired = []
    worker.idle_expired.connect(lambda: expired.append(True), Qt.ConnectionType.DirectConnection)
    began = time.monotonic()
    # WHEN: запускаем запись без приватных входов конвейера.
    await asyncio.wait_for(asyncio.to_thread(capture_transport, worker), 4)
    elapsed = time.monotonic() - began
    # THEN: подготовка VAD не теряет речь и не сдвигает начало тайм-аута.
    assert expired == [True] and worker.capture_finished.is_set() and not worker.sender_error
    # The model's preparation time must neither lose speech nor reset the clock.
    expected = (speech_packets - 1) * 0.1 + 1 if speech_packets else 1
    assert expected <= elapsed < expected + 0.4
    assert worker.captured_bytes / (2 * RATE) >= expected


async def test_disabled_idle_timeout_preserves_capture_and_recognition_pause_countdown(
    monkeypatch, capture_transport
):
    # GIVEN: автозавершение выключено, речь должна обновлять отсчёт распознавания.
    audio = np.full(2 * RATE, 0.2)
    monkeypatch.setattr(client, "read_audio", lambda path: audio)
    worker = client.DictationThread(Settings(stop_on_idle=False, idle_timeout=1), audio_file="test")
    countdown, expired, pauses = [], [], []
    worker.idle_remaining.connect(countdown.append, Qt.ConnectionType.DirectConnection)
    worker.idle_expired.connect(lambda: expired.append(True), Qt.ConnectionType.DirectConnection)
    worker.pause_remaining.connect(pauses.append, Qt.ConnectionType.DirectConnection)
    # WHEN: файл передаётся со скоростью записи микрофона.
    await asyncio.wait_for(asyncio.to_thread(capture_transport, worker), 3)
    # THEN: PCM захвачен целиком, жёлтый отсчёт работает, выключенный таймер не завершает запись.
    assert worker.capture_finished.is_set() and not worker.sender_error
    assert worker.captured_bytes == len(pcm(audio))
    assert not countdown and not expired
    assert any(remaining > 0 for remaining in pauses)


def test_enabling_idle_timeout_starts_a_fresh_countdown(monkeypatch, capture_clock, capture_transport):
    # GIVEN: тишина, выключенный таймер и переключения по управляемым часам.
    monkeypatch.setattr(client, "read_audio", lambda path: np.zeros(6 * 1600))
    worker = client.DictationThread(Settings(stop_on_idle=False, idle_timeout=1), audio_file="test")
    capture_clock(worker, [(100, True), (100.5, None), (200, False), (200, True), (201, None)])
    countdown, expired = [], []
    worker.idle_remaining.connect(countdown.append, Qt.ConnectionType.DirectConnection)
    worker.idle_expired.connect(lambda: expired.append(True), Qt.ConnectionType.DirectConnection)
    # WHEN: запись проходит включение, отключение и повторное включение таймера.
    capture_transport(worker)
    # THEN: каждый запуск даёт полный интервал, отключение не останавливает запись.
    assert countdown == [1, 0.5, 1, 0]
    assert expired == [True] and worker.capture_finished.is_set()


def test_pause_countdown_works_without_idle_stop_and_resets_on_new_speech(
    monkeypatch, capture_clock, capture_transport
):
    # GIVEN: два речевых блока разделены тишиной, таймер сессии первоначально выключен.
    audio = np.concatenate([np.full(1600, value) for value in (0, 0.2, 0, 0, 0, 0.2)])
    monkeypatch.setattr(client, "read_audio", lambda path: audio)
    monkeypatch.setattr(
        client,
        "SileroDetector",
        lambda path: SimpleNamespace(probability=lambda frame: float(np.mean(np.abs(frame)) > 0.05)),
    )
    worker = client.DictationThread(Settings(stop_on_idle=False, recognition_pause=3), audio_file="test")
    capture_clock(worker, [(100, None), (101.5, None), (101.5, True), (104, None), (104, None)])
    countdown = []
    worker.pause_remaining.connect(countdown.append, Qt.ConnectionType.DirectConnection)
    # WHEN: идёт запись, включается таймер сессии, затем возобновляется речь.
    capture_transport(worker)
    # THEN: жёлтый отсчёт независим от переключателя таймера и сбрасывается новой речью.
    # VAD отмечает речь на границах кадров 512 сэмплов (32 мс), а не PCM-пакетов 100 мс.
    assert countdown == pytest.approx([-1, 3, 1.5, 1.5, 0, 3], abs=512 / RATE)
    assert worker.capture_finished.is_set()


async def test_pause_mode_rejects_server_that_would_silently_use_fragmented_recognition(monkeypatch):
    monkeypatch.setattr(client, "read_audio", lambda path: np.zeros(RATE))
    monkeypatch.setattr(client, "health", lambda url: {"ready": True})

    # No PCM may be sent after a server fails to acknowledge the requested mode.
    async def old_server(socket):
        request = json.loads(await socket.recv())
        assert request["recognize_on_pause"] and request["recognition_pause"] == 3
        await socket.send('{"type":"ready"}')
        async for packet in socket:
            pytest.fail(f"Unexpected audio sent to unsupported server: {type(packet)}")

    async with serve(old_server, "127.0.0.1", 0) as listener:
        port = listener.sockets[0].getsockname()[1]
        worker = client.DictationThread(
            Settings(server_url=f"ws://127.0.0.1:{port}/v1/dictate"), audio_file="test"
        )
        failures = []
        worker.failure.connect(failures.append, Qt.ConnectionType.DirectConnection)
        await asyncio.wait_for(asyncio.to_thread(worker.run), 2)
    assert failures == ["Сервер не поддерживает распознавание после паузы. Обновите сервер"]
    assert worker.capture_finished.is_set()


async def test_server_records_buffered_audio_and_finalizes_it_before_idle_stop_completes(
    monkeypatch, tmp_path
):
    audio = np.concatenate([np.full(6400, 0.2), np.zeros(41600)])
    monkeypatch.setattr(client, "read_audio", lambda path: audio)
    began = time.monotonic()
    monkeypatch.setattr(client, "health", lambda url: {"ready": time.monotonic() - began >= 0.6})
    service = DictationServer(
        Settings(),
        recognizer=SimpleNamespace(decode=lambda audio: []),
        detector_factory=lambda: SimpleNamespace(
            probability=lambda frame: float(np.max(np.abs(frame)) > 0.05)
        ),
        home=tmp_path,
    )
    try:
        async with serve(service.handle, "127.0.0.1", 0) as listener:
            port = listener.sockets[0].getsockname()[1]
            worker = client.DictationThread(
                Settings(
                    server_url=f"ws://127.0.0.1:{port}/v1/dictate",
                    save_recordings=True,
                    idle_timeout=1,
                    load_on_demand=True,
                ),
                audio_file="test",
            )
            events, failures = [], []
            worker.event.connect(events.append, Qt.ConnectionType.DirectConnection)
            worker.failure.connect(failures.append, Qt.ConnectionType.DirectConnection)
            await asyncio.wait_for(asyncio.to_thread(worker.run), 4)
        assert not failures and events[-1]["type"] == "session_end"
        saved = [event for event in events if event["type"] == "recording"]
        assert [event["status"] for event in saved] == ["started", "saved"]
        directory = Path(saved[-1]["path"])
        with wave.open(str(directory / "audio.wav")) as recording:
            assert recording.getnframes() * 2 == worker.captured_bytes
            assert recording.readframes(len(audio)) == pcm(audio)[: worker.captured_bytes]
        assert 0 < worker.captured_bytes < len(pcm(audio))
        assert worker.startup_metrics["buffered_audio_seconds"] >= 0.5
        info = json.loads((directory / "session.json").read_text(encoding="utf-8"))
        assert info["status"] == "completed" and info["decode_count"] >= 1
    finally:
        service.pool.shutdown(wait=True)


async def test_recording_creation_error_is_reported_without_interrupting_client(monkeypatch):
    monkeypatch.setattr(client, "read_audio", lambda path: np.zeros(3200))
    monkeypatch.setattr(client, "health", lambda url: {"ready": True})

    async def handle(socket):
        assert json.loads(await socket.recv())["save_recordings"]
        await socket.send(
            json.dumps({"type": "ready", "recognize_on_pause": True, "recording_error": "no permissions"})
        )
        async for packet in socket:
            if not isinstance(packet, bytes):
                await socket.send('{"type":"session_end","text":""}')
                return

    async with serve(handle, "127.0.0.1", 0) as listener:
        port = listener.sockets[0].getsockname()[1]
        worker = client.DictationThread(
            Settings(server_url=f"ws://127.0.0.1:{port}/v1/dictate", save_recordings=True),
            audio_file="test",
        )
        events, failures = [], []
        worker.event.connect(events.append, Qt.ConnectionType.DirectConnection)
        worker.failure.connect(failures.append, Qt.ConnectionType.DirectConnection)
        await asyncio.wait_for(asyncio.to_thread(worker.run), 2)
    errors = [event for event in events if event["type"] == "recording"]
    assert len(errors) == 1 and errors[0]["status"] == "error"
    assert not failures and events[-1]["type"] == "session_end"


async def test_unconfirmed_recording_support_fails_before_sending_audio(monkeypatch):
    # GIVEN: сохранение включено, сервер не подтверждает архив и не сообщает ошибку диска.
    monkeypatch.setattr(client, "read_audio", lambda path: np.zeros(3200))
    monkeypatch.setattr(client, "health", lambda url: {"ready": True})
    received = []

    async def handle(socket):
        await socket.recv()
        await socket.send('{"type":"ready","recognize_on_pause":true}')
        async for packet in socket:
            received.append(packet)

    async with serve(handle, "127.0.0.1", 0) as listener:
        port = listener.sockets[0].getsockname()[1]
        worker = client.DictationThread(
            Settings(server_url=f"ws://127.0.0.1:{port}/v1/dictate", save_recordings=True), audio_file="test"
        )
        failures = []
        worker.failure.connect(failures.append, Qt.ConnectionType.DirectConnection)
        # WHEN: клиент начинает сессию через реальный WebSocket.
        await asyncio.wait_for(asyncio.to_thread(worker.run), 2)
    # THEN: PCM не передаётся, микрофон закрывается, ошибка не маскируется успешной диктовкой.
    assert failures == ["Сервер не подтвердил сохранение записи. Перезапустите или обновите сервер"]
    assert received == [] and worker.capture_finished.is_set()


@pytest.mark.parametrize(
    ("settings", "error"),
    [
        pytest.param(Settings(load_on_demand=True), URLError(TimeoutError("timeout")), id="timeout"),
        pytest.param(
            Settings(load_on_demand=True),
            HTTPError("http://local", 503, "unavailable", {}, None),
            id="http-error",
        ),
        pytest.param(Settings(load_on_demand=True), ValueError("wrong protocol"), id="invalid-health"),
        pytest.param(Settings(), URLError(ConnectionRefusedError("refused")), id="eager-server-refused"),
        pytest.param(
            Settings(server_url="ws://remote.example/v1/dictate", load_on_demand=True),
            URLError(ConnectionRefusedError("refused")),
            id="remote-server-refused",
        ),
    ],
)
async def test_health_failure_stops_capture_without_retry(monkeypatch, settings, error):
    # GIVEN: health недоступен по причине, не относящейся к запуску своего listener.
    requests, failures = [], []
    monkeypatch.setattr(client, "read_audio", lambda path: np.zeros(0))

    def failed(url):
        requests.append(url)
        raise error

    monkeypatch.setattr(client, "health", failed)
    worker = client.DictationThread(settings, audio_file="test")
    worker.failure.connect(failures.append, Qt.ConnectionType.DirectConnection)
    # WHEN: клиент начинает диктовку.
    await asyncio.wait_for(asyncio.to_thread(worker.run), 2)
    # THEN: один запрос, видимая исходная ошибка и закрытый захват.
    assert requests == [settings.server_url] and failures == [str(error)]
    assert worker.capture_finished.is_set() and not worker.startup_metrics


async def test_model_startup_error_stops_capture_without_retry(monkeypatch):
    # GIVEN: сервер сообщает явную ошибку загрузки модели вместо состояния ожидания.
    requests, failures = [], []
    monkeypatch.setattr(client, "read_audio", lambda path: np.zeros(0))

    def failed(url):
        requests.append(url)
        return {"ready": False, "error": "Не удалось загрузить модель"}

    monkeypatch.setattr(client, "health", failed)
    worker = client.DictationThread(Settings(load_on_demand=True), audio_file="test")
    worker.failure.connect(failures.append, Qt.ConnectionType.DirectConnection)
    # WHEN: клиент начинает диктовку.
    await asyncio.wait_for(asyncio.to_thread(worker.run), 2)
    # THEN: ошибка видна после одной проверки, захват закрыт, подключения и передачи PCM нет.
    assert requests == [worker.settings.server_url]
    assert failures == ["Не удалось загрузить модель"]
    assert worker.capture_finished.is_set() and not worker.startup_metrics


async def test_local_on_demand_connection_refusal_waits_for_listener_without_resending_audio(monkeypatch):
    # GIVEN: свой сервер открывает listener после начала захвата.
    audio = np.full(3200, 0.2)
    monkeypatch.setattr(client, "read_audio", lambda path: audio)
    requests, packets, failures = [], [], []

    def loading(url):
        requests.append(url)
        if len(requests) == 1:
            raise URLError(ConnectionRefusedError("refused"))
        return {"ready": True}

    monkeypatch.setattr(client, "health", loading)

    async def handle(socket):
        await socket.recv()
        await socket.send('{"type":"ready","recognize_on_pause":true}')
        async for packet in socket:
            if isinstance(packet, bytes):
                packets.append(packet)
            else:
                await socket.send('{"type":"session_end","text":""}')
                return

    async with serve(handle, "127.0.0.1", 0) as listener:
        port = listener.sockets[0].getsockname()[1]
        worker = client.DictationThread(
            Settings(server_url=f"ws://127.0.0.1:{port}/v1/dictate", load_on_demand=True), audio_file="test"
        )
        worker.failure.connect(failures.append, Qt.ConnectionType.DirectConnection)
        # WHEN: клиент дожидается listener и завершает запись.
        await asyncio.wait_for(asyncio.to_thread(worker.run), 2)
    # THEN: повторяется только health; каждый PCM-сэмпл отправлен ровно один раз.
    assert len(requests) == 2 and failures == []
    assert b"".join(packets) == pcm(audio) and worker.capture_finished.is_set()


def test_model_readiness_wait_has_a_finite_deadline(monkeypatch):
    # GIVEN: модель никогда не становится готова; часы продвигаются без реального ожидания.
    now, requests, failures = [0.0], [], []

    def monotonic():
        now[0] += 31
        return now[0]

    monkeypatch.setattr(client, "time", SimpleNamespace(monotonic=monotonic))
    monkeypatch.setattr(client, "read_audio", lambda path: np.zeros(0))
    monkeypatch.setattr(client, "health", lambda url: requests.append(url) or {"ready": False})
    worker = client.DictationThread(Settings(load_on_demand=True), audio_file="test")
    worker.failure.connect(failures.append, Qt.ConnectionType.DirectConnection)
    # WHEN: истекает бюджет запуска модели.
    worker.run()
    # THEN: ожидание прекращается явной ошибкой, захват закрывается.
    assert failures == ["Модель не подготовилась за 60 секунд. Проверьте .data/server.log"]
    assert len(requests) == 2 and worker.capture_finished.is_set()


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param({"recording_error": 7}, id="archive-error-is-not-text"),
        pytest.param({"recording_path": ["folder"]}, id="archive-path-is-not-text"),
    ],
)
async def test_invalid_ready_fails_before_pcm_or_ui_events(monkeypatch, reply):
    # GIVEN: настоящий WebSocket подтверждает режим, но возвращает неверные поля архива.
    monkeypatch.setattr(client, "read_audio", lambda path: np.zeros(1600))
    monkeypatch.setattr(client, "health", lambda url: {"ready": True})
    received, events, failures = [], [], []

    async def handler(socket):
        received.append(json.loads(await socket.recv()))
        await socket.send(json.dumps({"type": "ready", "recognize_on_pause": True, **reply}))
        async for packet in socket:
            received.append(packet)
            if isinstance(packet, str):
                await socket.send('{"type":"session_end","text":""}')

    async with serve(handler, "127.0.0.1", 0) as server:
        worker = client.DictationThread(
            Settings(server_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", save_recordings=True),
            audio_file="test",
        )
        worker.event.connect(events.append, Qt.ConnectionType.DirectConnection)
        worker.failure.connect(failures.append, Qt.ConnectionType.DirectConnection)
        # WHEN: клиент принимает ошибочное подтверждение.
        await asyncio.wait_for(asyncio.to_thread(worker.run), 3)
    # THEN: виден отказ протокола; аудио и ошибочное событие не достигают потребителей.
    assert len(failures) == 1 and "Некорректный ответ сервера" in failures[0]
    assert len(received) == 1 and received[0]["type"] == "start"
    assert events == [] and worker.capture_finished.is_set()


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param({"type": "recording", "status": "error", "message": 7}, id="invalid-recording-message"),
        pytest.param({"type": "session_end", "text": 7}, id="invalid-terminal-text"),
        pytest.param({"type": "loading", "seconds": "bad"}, id="network-cannot-emit-local-ui-event"),
    ],
)
async def test_invalid_session_reply_preserves_confirmed_commit_and_stops(monkeypatch, reply):
    # GIVEN: сервер уже передал один корректный commit, затем нарушает wire-контракт.
    monkeypatch.setattr(client, "read_audio", lambda path: np.zeros(1600))
    monkeypatch.setattr(client, "health", lambda url: {"ready": True})
    events, failures = [], []
    commit = {"type": "commit", "seq": 1, "delta": "Подтверждённый текст."}

    async def handler(socket):
        await socket.recv()
        await socket.send('{"type":"ready","recognize_on_pause":true}')
        async for packet in socket:
            if isinstance(packet, str):
                await socket.send(json.dumps(commit))
                await socket.send(json.dumps(reply))
                if reply["type"] != "session_end":
                    await socket.send('{"type":"session_end","text":"Подтверждённый текст."}')
                break

    async with serve(handler, "127.0.0.1", 0) as server:
        worker = client.DictationThread(
            Settings(server_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"), audio_file="test"
        )
        worker.event.connect(events.append, Qt.ConnectionType.DirectConnection)
        worker.failure.connect(failures.append, Qt.ConnectionType.DirectConnection)
        # WHEN: клиент получает повреждённый ответ.
        await asyncio.wait_for(asyncio.to_thread(worker.run), 3)
    # THEN: подтверждённый текст доставлен один раз, неверный ответ не передан в Qt.
    assert len(failures) == 1 and "Некорректный ответ сервера" in failures[0]
    assert [event for event in events if event["type"] != "model_ready"] == [commit]
    assert worker.capture_finished.is_set()
