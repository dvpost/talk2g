import asyncio
import json
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest
import pytest_asyncio
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import InvalidStatus

from talk2g import server as server_module
from talk2g.audio import pcm
from talk2g.config import RATE, Settings
from talk2g.model import Word
from talk2g.recording import SessionRecording
from talk2g.server import DictationServer


class Detector:
    def probability(self, frame):
        return float(np.max(np.abs(frame)) > 0.01)


class Recognizer:
    def decode(self, audio):
        time.sleep(0.03)
        words = [Word("Привет,", 0.2, 0.4), Word("это", 0.6, 0.8), Word("проверка.", 1, 1.2)]
        return [word for word in words if word.end <= len(audio) / RATE]


@pytest_asyncio.fixture
async def server(tmp_path):
    service = DictationServer(
        Settings(interval=0.25, holdback=0.2, recognize_on_pause=False),
        recognizer=Recognizer(),
        detector_factory=Detector,
        home=tmp_path,
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


async def send_audio(socket, audio):
    for offset in range(0, len(audio), RATE):
        await socket.send(pcm(audio[offset : offset + RATE]))


async def test_pause_mode_waits_for_silence_commits_once_and_flushes_stop_tail(server):
    service, url = server
    inputs, progress = [], []
    original = service.recognizer.decode

    def decode(audio):
        inputs.append(audio.copy())
        return original(audio)

    service.recognizer.decode = decode
    async with connect(url, proxy=None) as socket:
        ready = await start(
            socket, recognize_on_pause=True, recognition_pause=1, window=4, save_recordings=True
        )
        assert ready["recognize_on_pause"] and ready["recognition_pause"] == 1
        receiver = asyncio.create_task(collect(socket, progress))
        await send_audio(socket, np.full(7 * RATE, 0.2))
        await asyncio.sleep(0.35)  # longer than legacy cadence, speech exceeds legacy window
        assert not inputs and not progress
        await send_audio(socket, np.zeros(int(0.6 * RATE)))
        await send_audio(socket, np.full(int(1.2 * RATE), 0.2))
        await asyncio.sleep(0.35)
        assert not inputs and not progress
        await send_audio(socket, np.zeros(int(1.1 * RATE)))
        async with asyncio.timeout(2):
            while not any(event["type"] == "segment_end" for event in progress):
                await asyncio.sleep(0.01)
        assert len(inputs) == 1 and 9 <= len(inputs[0]) / RATE < 9.2
        assert sum(event["type"] == "commit" for event in progress) == 1
        # Speech following the recognition pause becomes a separate whole block.
        tail = np.full(int(1.2 * RATE) + 207, 0.3)
        await send_audio(socket, tail)
        await socket.send('{"type":"stop"}')
        events = await asyncio.wait_for(receiver, 2)
    assert len(inputs) == 2
    np.testing.assert_array_equal(inputs[1][-len(tail) :], np.frombuffer(pcm(tail), dtype="<i2") / 32768)
    assert not any(event["type"] == "partial" for event in events)
    commits = [event for event in events if event["type"] == "commit"]
    assert [event["seq"] for event in commits] == [1, 2]
    assert events[-1]["text"] == "Привет, это проверка. Привет, это проверка."
    assert "".join(event["delta"] for event in commits) == events[-1]["text"]
    directory = Path(events[-1]["recording_path"])
    info = json.loads((directory / "session.json").read_text(encoding="utf-8"))
    assert info["settings"]["recognize_on_pause"] and info["settings"]["recognition_pause"] == 1
    journal = [
        json.loads(line) for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    ranges = [event for event in journal if event["type"] == "decode_start"]
    assert info["decode_count"] == len(ranges) == 2
    assert ranges[0]["end_sample"] <= ranges[1]["start_sample"]
    with wave.open(str(directory / "audio.wav")) as recording:
        received = (
            np.frombuffer(recording.readframes(recording.getnframes()), dtype="<i2").astype(np.float32)
            / 32768
        )
    for event, model_input in zip(ranges, inputs, strict=True):
        np.testing.assert_array_equal(model_input, received[event["start_sample"] : event["end_sample"]])


async def test_pause_mode_keeps_new_audio_while_previous_block_is_recognized(server):
    service, url = server
    entered, release = threading.Event(), threading.Event()
    original = service.recognizer.decode
    inputs = []

    def blocking(audio):
        inputs.append(audio.copy())
        if len(inputs) == 1:
            entered.set()
            assert release.wait(timeout=3)
        return original(audio)

    service.recognizer.decode = blocking
    try:
        async with connect(url, proxy=None) as socket:
            await start(socket, recognize_on_pause=True, recognition_pause=0.5)
            await send_audio(socket, np.full(2 * RATE, 0.2))
            await send_audio(socket, np.zeros(RATE))
            assert await asyncio.to_thread(entered.wait, 2)
            await send_audio(socket, np.full(2 * RATE + 123, 0.3))
            await socket.send('{"type":"stop"}')
            await asyncio.sleep(0.05)
            release.set()
            events = await asyncio.wait_for(collect(socket), 3)
        assert len(inputs) == 2
        assert events[-1]["text"] == "Привет, это проверка. Привет, это проверка."
        assert events[-1]["audio_seconds"] == (5 * RATE + 123) / RATE
        assert sum(event["type"] == "segment_end" for event in events) == 2
    finally:
        release.set()


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"unknown_option": True, "unknown_number": 16},
    ],
)
async def test_real_duplex_protocol_commits_before_stop_and_flushes_tail(server, options):
    _, url = server
    async with connect(url, proxy=None) as socket:
        ready = await start(socket, **options)
        assert ready["type"] == "ready"
        assert ready == {"type": "ready", "model": "gigaam-v3-e2e-ctc", "session_id": ""}
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
    service, url = server
    async with connect(url, proxy=None) as socket:
        await start(socket)
        await socket.send(pcm(np.zeros(1600)))
        await socket.send('{"type":"stop"}')
        result = await asyncio.wait_for(collect(socket), 2)
    assert result[-1]["type"] == "session_end"
    assert result[-1]["text"] == ""
    assert not (service.home / "recordings").exists()


@pytest.mark.parametrize(
    "options",
    [
        {"version": 2},
        {"rate": 48000},
        {"interval": float("nan")},
        {"window": 100},
        {"save_recordings": "true"},
        {"recognize_on_pause": "true"},
        {"recognition_pause": 0},
        {"recognition_pause": float("inf")},
    ],
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


async def test_recording_preserves_received_pcm_and_every_actual_model_input(server):
    service, url = server
    inputs = []
    original = service.recognizer.decode

    def decode(audio):
        inputs.append(audio.copy())
        return original(audio)

    service.recognizer.decode = decode
    audio = np.concatenate([np.zeros(1600), np.linspace(0.05, 0.4, 5 * RATE), np.zeros(10207)])
    packet = pcm(audio)
    async with connect(url, proxy=None) as socket:
        ready = await start(socket, save_recordings=True, session_id="../../outside", window=4)
        directory = Path(ready["recording_path"])
        assert directory.parent == service.home / "recordings"
        for offset in range(0, len(packet), 12800):
            await socket.send(packet[offset : offset + 12800])
            await asyncio.sleep(0.025)
        await socket.send('{"type":"stop"}')
        events = await asyncio.wait_for(collect(socket), 3)
        # Files must be finalized before session_end permits on-demand shutdown.
        with wave.open(str(directory / "audio.wav")) as recording:
            assert recording.getnchannels() == 1 and recording.getsampwidth() == 2
            assert recording.getframerate() == RATE and recording.getnframes() == len(audio)
            assert recording.readframes(len(audio)) == packet
    assert events[-1]["recording_path"] == str(directory)
    info = json.loads((directory / "session.json").read_text(encoding="utf-8"))
    assert info["status"] == "completed" and info["text"] == events[-1]["text"]
    assert info["audio_samples"] == len(audio) and info["settings"]["window"] == 4
    journal = [
        json.loads(line) for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    ranges = [event for event in journal if event["type"] == "decode_start"]
    assert len(ranges) == info["decode_count"] == len(inputs) >= 2
    received = np.frombuffer(packet, dtype="<i2").astype(np.float32) / 32768
    for event, model_input in zip(ranges, inputs, strict=True):
        np.testing.assert_array_equal(model_input, received[event["start_sample"] : event["end_sample"]])
    assert any(event["start_sample"] > 0 for event in ranges)  # trimming and overlap retain correct offsets
    assert any(event["type"] == "decode_end" and event["forced"] for event in journal)
    assert "token" not in info and "token" not in info["settings"]


@pytest.mark.parametrize("outcome", ["cancelled", "disconnected", "error"])
async def test_recordings_are_finalized_on_cancel_disconnect_and_decoder_failure(server, outcome):
    service, url = server
    if outcome == "error":

        def broken(audio):
            raise RuntimeError("decoder failed")

        service.recognizer.decode = broken
    packet = pcm(np.full(RATE + 1600, 0.2))
    async with connect(url, proxy=None) as socket:
        ready = await start(socket, save_recordings=True)
        directory = Path(ready["recording_path"])
        await socket.send(packet[: 2 * RATE])
        await socket.send(packet[2 * RATE :])
        if outcome == "cancelled":
            await socket.send('{"type":"cancel"}')
            assert (await collect(socket))[-1]["type"] == "cancelled"
        elif outcome == "error":
            assert (await asyncio.wait_for(collect(socket), 2))[-1]["type"] == "error"
    for _ in range(100):
        if not service.active:
            break
        await asyncio.sleep(0.01)
    assert not service.active
    info = json.loads((directory / "session.json").read_text(encoding="utf-8"))
    assert info["status"] == outcome
    with wave.open(str(directory / "audio.wav")) as recording:
        assert recording.readframes(len(packet) // 2) == packet


async def test_recording_creation_failure_does_not_stop_dictation(server, monkeypatch):
    _, url = server

    def denied(*args):
        raise PermissionError("recordings are read-only")

    monkeypatch.setattr(server_module, "SessionRecording", denied)
    async with connect(url, proxy=None) as socket:
        ready = await start(socket, save_recordings=True)
        assert ready["type"] == "ready" and "read-only" in ready["recording_error"]
        await socket.send(pcm(np.full(RATE, 0.2)))
        await socket.send(pcm(np.full(8000, 0.2)))
        await socket.send('{"type":"stop"}')
        events = await asyncio.wait_for(collect(socket), 2)
    assert events[-1]["type"] == "session_end" and events[-1]["text"] == "Привет, это проверка."


async def test_recording_write_failure_is_reported_once_and_does_not_stop_dictation(server, monkeypatch):
    _, url = server

    def full_disk(data):
        raise OSError("disk full")

    def recording(*args):
        result = SessionRecording(*args)
        monkeypatch.setattr(result.audio, "writeframesraw", full_disk)
        return result

    monkeypatch.setattr(server_module, "SessionRecording", recording)
    async with connect(url, proxy=None) as socket:
        await start(socket, save_recordings=True)
        await socket.send(pcm(np.full(RATE, 0.2)))
        await socket.send(pcm(np.full(8000, 0.2)))
        await socket.send('{"type":"stop"}')
        events = await asyncio.wait_for(collect(socket), 2)
    errors = [event for event in events if event["type"] == "recording"]
    assert len(errors) == 1 and errors[0]["status"] == "error" and errors[0]["message"] == "disk full"
    assert events[-1]["type"] == "session_end" and events[-1]["text"] == "Привет, это проверка."
    assert "recording_path" not in events[-1]
