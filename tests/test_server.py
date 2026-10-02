import asyncio
import json
import threading
import time

import numpy as np
import pytest
import pytest_asyncio
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import InvalidStatus

from giga_dictation.audio import pcm
from giga_dictation.config import RATE, Settings
from giga_dictation.model import Word
from giga_dictation.server import DictationServer


class Detector:
    def probability(self, frame):
        return float(np.max(np.abs(frame)) > 0.01)


class Recognizer:
    def decode(self, audio):
        time.sleep(0.03)
        words = [Word("Привет,", 0.2, 0.4), Word("это", 0.6, 0.8), Word("проверка.", 1, 1.2)]
        return [word for word in words if word.end <= len(audio) / RATE]


@pytest_asyncio.fixture
async def server():
    service = DictationServer(
        Settings(interval=0.25, holdback=0.2), recognizer=Recognizer(), detector_factory=Detector
    )
    async with serve(service.handle, "127.0.0.1", 0, process_request=service.health) as listener:
        port = listener.sockets[0].getsockname()[1]
        yield service, f"ws://127.0.0.1:{port}/v1/dictate"
    service.pool.shutdown(wait=True)


async def start(socket, **options):
    message = {"type": "start", "version": 1, "rate": RATE, "format": "pcm16"} | options
    await socket.send(json.dumps(message))
    return json.loads(await socket.recv())


async def collect(socket, result=None):
    if result is None:
        result = []
    async for message in socket:
        event = json.loads(message)
        result.append(event)
        if event["type"] in ("session_end", "cancelled", "error"):
            return result
    return result


async def test_real_duplex_protocol_commits_before_stop_and_flushes_tail(server):
    _, url = server
    async with connect(url, proxy=None) as socket:
        assert (await start(socket))["type"] == "ready"
        progress = []
        receive = asyncio.create_task(collect(socket, progress))
        for _ in range(18):
            await socket.send(pcm(np.full(1600, 0.2)))
            await asyncio.sleep(0.06)
        assert not receive.done()
        # Allow the cadence to deliver a running-window commit.
        await asyncio.sleep(0.3)
        assert any(event["type"] == "commit" for event in progress)
        await socket.send('{"type":"stop"}')
        events = await asyncio.wait_for(receive, 3)
    commits = [event for event in events if event["type"] == "commit"]
    assert len(commits) >= 2
    assert "".join(event["delta"] for event in commits) == "Привет, это проверка."
    assert events[-1]["type"] == "session_end"
    assert events[-1]["audio_seconds"] == 1.8
    assert events[-1]["text"] == "Привет, это проверка."


async def test_stop_without_speech_returns_empty_result(server):
    _, url = server
    async with connect(url, proxy=None) as socket:
        await start(socket)
        await socket.send(pcm(np.zeros(1600)))
        await socket.send('{"type":"stop"}')
        result = await asyncio.wait_for(collect(socket), 2)
    assert result[-1]["type"] == "session_end"
    assert result[-1]["text"] == ""


@pytest.mark.parametrize(
    "options", [{"version": 2}, {"rate": 48000}, {"interval": float("nan")}, {"window": 100}]
)
async def test_invalid_protocol_and_parameters_are_refused(server, options):
    _, url = server
    async with connect(url, proxy=None) as socket:
        assert (await start(socket, **options))["type"] == "error"


async def test_authentication_and_admission(server):
    service, url = server
    service.settings.token = "test-only-token"
    async with connect(url, proxy=None) as refused:
        assert (await start(refused))["type"] == "error"
    async with connect(url, proxy=None) as first:
        assert (await start(first, token="test-only-token"))["type"] == "ready"
        async with connect(url, proxy=None) as second:
            assert "занят" in (await start(second, token="test-only-token"))["message"]
        await first.send('{"type":"cancel"}')
        assert json.loads(await first.recv())["type"] == "cancelled"


async def test_browser_origin_is_refused(server):
    _, url = server
    with pytest.raises(InvalidStatus):
        async with connect(url, origin="https://untrusted.example", proxy=None):
            pass


async def test_decoder_failure_reaches_client_without_waiting_for_more_audio(server):
    service, url = server

    def broken(audio):
        raise RuntimeError("decoder failed")

    service.recognizer.decode = broken
    async with connect(url, proxy=None) as socket:
        await start(socket)
        await socket.send(pcm(np.full(RATE, 0.2)))
        await socket.send(pcm(np.full(1600, 0.2)))
        result = await asyncio.wait_for(collect(socket), 2)
    assert result[-1]["type"] == "error"
    assert result[-1]["message"] == "decoder failed"


async def test_malformed_audio_packet_does_not_leak_admission(server):
    service, url = server
    async with connect(url, proxy=None) as socket:
        await start(socket)
        await socket.send(b"x")
        assert (await collect(socket))[-1]["type"] == "error"
    assert not service.active


async def test_stop_during_inference_preserves_audio_arriving_in_parallel(server):
    service, url = server
    entered, release = threading.Event(), threading.Event()
    original = service.recognizer.decode
    first = True

    def blocking(audio):
        nonlocal first
        if first:
            first = False
            entered.set()
            assert release.wait(timeout=3)
        return original(audio)

    service.recognizer.decode = blocking
    try:
        async with connect(url, proxy=None) as socket:
            await start(socket)
            await socket.send(pcm(np.full(RATE, 0.2)))
            await socket.send(pcm(np.full(1600, 0.2)))
            assert await asyncio.to_thread(entered.wait, 2)
            await socket.send(pcm(np.full(8000, 0.2)))
            await socket.send('{"type":"stop"}')
            await asyncio.sleep(0.05)
            release.set()
            events = await asyncio.wait_for(collect(socket), 3)
        assert events[-1]["text"] == "Привет, это проверка."
        assert events[-1]["audio_seconds"] == 1.6
        assert sum(event["type"] == "segment_end" for event in events) == 1
    finally:
        release.set()


async def test_disconnect_releases_server_for_next_dictation(server):
    service, url = server
    async with connect(url, proxy=None) as socket:
        await start(socket)
        await socket.send(pcm(np.full(1600, 0.2)))
    for _ in range(50):
        if not service.active:
            break
        await asyncio.sleep(0.01)
    assert not service.active
    async with connect(url, proxy=None) as socket:
        assert (await start(socket))["type"] == "ready"
        await socket.send('{"type":"stop"}')
        assert (await collect(socket))[-1]["type"] == "session_end"
