import asyncio
import json
import queue
import threading
import time

import numpy as np
import pytest
from PySide6.QtCore import Qt
from websockets.asyncio.server import serve

from talk2g import client
from talk2g.audio import pcm
from talk2g.config import RATE, Settings


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
        await socket.send('{"type":"ready"}')
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
        await socket.send('{"type":"ready"}')
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
