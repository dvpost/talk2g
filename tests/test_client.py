import asyncio
import json
import queue
import sys
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import Qt
from websockets.asyncio.server import serve

from talk2g import client
from talk2g.audio import SpeechTimeout, pcm
from talk2g.config import RATE, Settings
from talk2g.server import DictationServer


@pytest.fixture(autouse=True)
def fake_vad(monkeypatch):
    class Detector:
        def probability(self, frame):
            return float(np.max(np.abs(frame)) > 0.05)

    monkeypatch.setattr(client, "prepare_vad", lambda: None)
    monkeypatch.setattr(client, "SileroDetector", lambda path: Detector())


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


async def test_new_speech_resets_timeout_and_long_speech_keeps_capture_running(monkeypatch):
    audio = np.concatenate([np.zeros(6400), np.full(19200, 0.2), np.zeros(22400)])
    monkeypatch.setattr(client, "read_audio", lambda path: audio)
    worker = client.DictationThread(Settings(idle_timeout=1), audio_file="test")
    countdown, expired = [], []
    worker.idle_remaining.connect(countdown.append, Qt.ConnectionType.DirectConnection)
    worker.idle_expired.connect(lambda: expired.append(True), Qt.ConnectionType.DirectConnection)
    await asyncio.wait_for(asyncio.to_thread(worker._capture), 4)
    assert worker.capture_finished.is_set() and expired == [True]
    assert 2.5 <= worker.captured_bytes / (2 * RATE) <= 2.9
    assert min(countdown[:4]) < 0.8
    assert min(countdown[5:16]) > 0.9  # speech longer than the timeout keeps resetting it
    assert countdown[-1] == 0


@pytest.mark.parametrize("speech_packets,vad_delay", [(0, 0), (14, 1.2)])
async def test_microphone_timeout_handles_initial_silence_and_delayed_vad(
    monkeypatch, speech_packets, vad_delay
):
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
    await asyncio.wait_for(asyncio.to_thread(worker._capture), 4)
    elapsed = time.monotonic() - began
    assert expired == [True] and worker.capture_finished.is_set() and not worker.sender_error
    # The model's preparation time must neither lose speech nor reset the clock.
    expected = (speech_packets - 1) * 0.1 + 1 if speech_packets else 1
    assert expected <= elapsed < expected + 0.4
    assert worker.captured_bytes / (2 * RATE) >= expected


async def test_disabled_idle_timeout_captures_all_audio_without_loading_vad(monkeypatch):
    audio = np.zeros(2 * RATE)
    monkeypatch.setattr(client, "read_audio", lambda path: audio)

    def unexpected_vad():
        raise AssertionError("Disabled timeout must not load VAD")

    monkeypatch.setattr(client, "prepare_vad", unexpected_vad)
    worker = client.DictationThread(
        Settings(stop_on_idle=False, idle_timeout=1, recognize_on_pause=False), audio_file="test"
    )
    countdown, expired = [], []
    worker.idle_remaining.connect(countdown.append, Qt.ConnectionType.DirectConnection)
    worker.idle_expired.connect(lambda: expired.append(True), Qt.ConnectionType.DirectConnection)
    await asyncio.wait_for(asyncio.to_thread(worker._capture), 3)
    assert worker.capture_finished.is_set() and not worker.sender_error
    assert worker.captured_bytes == len(pcm(audio))
    assert not countdown and not expired


def test_enabling_idle_timeout_starts_a_fresh_countdown(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(client.time, "monotonic", lambda: now[0])
    worker = client.DictationThread(Settings(stop_on_idle=False, idle_timeout=1))
    timeout = SpeechTimeout(None, 1, started_at=0)
    countdown, expired = [], []
    worker.idle_remaining.connect(countdown.append, Qt.ConnectionType.DirectConnection)
    worker.idle_expired.connect(lambda: expired.append(True), Qt.ConnectionType.DirectConnection)
    worker._check_idle_timeout(timeout)
    assert not countdown and not expired
    worker.set_idle_enabled(True)
    worker._check_idle_timeout(timeout)
    assert countdown[-1] == 1 and not expired
    now[0] = 100.5
    worker._check_idle_timeout(timeout)
    assert countdown[-1] == 0.5 and not expired
    worker.set_idle_enabled(False)
    now[0] = 200
    worker._check_idle_timeout(timeout)
    assert countdown[-1] == 0.5 and not expired
    worker.set_idle_enabled(True)
    worker._check_idle_timeout(timeout)
    assert countdown[-1] == 1 and not expired
    now[0] = 201
    worker._check_idle_timeout(timeout)
    assert countdown[-1] == 0 and expired == [True] and worker.stop_requested.is_set()


def test_pause_countdown_works_without_idle_stop_and_resets_on_new_speech(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(client.time, "monotonic", lambda: now[0])
    worker = client.DictationThread(Settings(stop_on_idle=False, recognition_pause=3))
    timeout = worker._speech_timeout()
    timeout.last_speech = now[0]
    countdown = []
    worker.pause_remaining.connect(countdown.append, Qt.ConnectionType.DirectConnection)
    worker._check_idle_timeout(timeout)
    assert countdown == [-1]  # no pending speech before the first voiced block
    timeout.feed(pcm(np.full(512 * 3, 0.2)), captured_at=100)
    worker._check_idle_timeout(timeout)
    assert countdown[-1] == 3
    now[0] = 101.5
    worker._check_idle_timeout(timeout)
    assert countdown[-1] == 1.5
    worker.set_idle_enabled(True)
    worker._check_idle_timeout(timeout)
    assert countdown[-1] == 1.5 and timeout.last_speech == 100
    now[0] = 104
    worker._check_idle_timeout(timeout)
    assert countdown[-1] == 0 and not worker.stop_requested.is_set()
    timeout.feed(pcm(np.full(512 * 3, 0.2)), captured_at=104)
    worker._check_idle_timeout(timeout)
    assert countdown[-1] == 3
    worker.stop()
    worker._check_idle_timeout(timeout)
    assert len(countdown) == 6


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


@pytest.mark.parametrize("ready_options", [{}, {"recording_error": "no permissions"}])
async def test_old_server_or_recording_creation_error_does_not_interrupt_client(monkeypatch, ready_options):
    monkeypatch.setattr(client, "read_audio", lambda path: np.zeros(3200))
    monkeypatch.setattr(client, "health", lambda url: {"ready": True})

    async def handle(socket):
        assert json.loads(await socket.recv())["save_recordings"]
        await socket.send(json.dumps({"type": "ready"} | ready_options))
        async for packet in socket:
            if not isinstance(packet, bytes):
                await socket.send('{"type":"session_end","text":""}')
                return

    async with serve(handle, "127.0.0.1", 0) as listener:
        port = listener.sockets[0].getsockname()[1]
        worker = client.DictationThread(
            Settings(
                server_url=f"ws://127.0.0.1:{port}/v1/dictate", save_recordings=True, recognize_on_pause=False
            ),
            audio_file="test",
        )
        events, failures = [], []
        worker.event.connect(events.append, Qt.ConnectionType.DirectConnection)
        worker.failure.connect(failures.append, Qt.ConnectionType.DirectConnection)
        await asyncio.wait_for(asyncio.to_thread(worker.run), 2)
    errors = [event for event in events if event["type"] == "recording"]
    assert len(errors) == 1 and errors[0]["status"] == "error"
    assert not failures and events[-1]["type"] == "session_end"
