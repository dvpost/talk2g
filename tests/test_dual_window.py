import asyncio
import json
import threading

import numpy as np
import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from giga_dictation.audio import pcm
from giga_dictation.config import RATE, Settings
from giga_dictation.dual_window import WindowAgreement
from giga_dictation.model import Word
from giga_dictation.server import DictationServer


def test_disputed_word_is_not_inserted_and_long_context_final_can_correct_it():
    windows = WindowAgreement(0.2)
    fast = [Word("Это", 0, 0.2), Word("кот", 0.5, 0.7), Word("работает", 1, 1.2)]
    quality = [Word("Это", 0, 0.2), Word("код", 0.5, 0.7), Word("работает.", 1, 1.2)]
    assert windows.preview(fast, 0, 2) == "Это кот работает"
    assert windows.quality(quality, 2)[0] == "Это"
    assert windows.transcript.text == "Это" and windows.disagreements == 1
    assert windows.quality(quality, 2, final=True)[0] == " код работает."
    assert windows.transcript.text == "Это код работает."


def test_new_fast_context_resolves_disagreement_without_waiting_for_stop():
    windows = WindowAgreement(0.2)
    bad = [Word("кот", 0, 0.2), Word("работает", 0.6, 0.8)]
    good = [Word("код", 0, 0.2), Word("работает", 0.6, 0.8)]
    windows.preview(bad, 0, 1)
    assert windows.quality(good, 1)[0] == ""
    windows.preview(good, 0, 1.5)
    assert windows.quality(good, 1.5)[0] == "код"


def test_old_fast_snapshots_cover_words_before_the_short_window_start():
    windows = WindowAgreement(0.2)
    first = [Word("Первое", 0, 0.2), Word("второе", 1, 1.2)]
    later = [Word("третье", 4, 4.2), Word("четвёртое", 5, 5.2)]
    windows.preview(first, 0, 2)
    windows.preview(later, 3, 6)
    assert windows.quality(first + later, 6)[0] == "Первое второе третье"


def test_repeated_words_use_distinct_time_anchors():
    windows = WindowAgreement(0.2)
    words = [Word("Да,", 0, 0.1), Word("да,", 0.3, 0.4), Word("да.", 0.8, 0.9)]
    windows.preview(words, 0, 2)
    assert windows.quality(words, 2)[0] == "Да, да,"
    assert windows.quality(words, 2, final=True)[0] == " да."
    assert windows.quality(words, 2, final=True)[0] == ""


class Detector:
    def probability(self, frame):
        return float(np.max(np.abs(frame)) > 0.01)


class Recognizer:
    def __init__(self):
        self.durations = []

    def decode(self, audio):
        self.durations.append(len(audio) / RATE)
        # The monotonic test signal encodes absolute audio time, independent of window boundaries.
        offset = round((float(audio[0]) - 0.1) / 0.01, 1)
        end = offset + len(audio) / RATE
        return [
            Word(f"слово{i}", i + 0.2 - offset, i + 0.4 - offset)
            for i in range(10)
            if offset <= i + 0.2 and i + 0.4 <= end
        ]


async def collect(socket):
    events = []
    async for message in socket:
        event = json.loads(message)
        events.append(event)
        if event["type"] in ("session_end", "cancelled", "error"):
            return events


async def start(socket, **overrides):
    await socket.send(
        json.dumps(
            {
                "type": "start",
                "version": 1,
                "rate": RATE,
                "format": "pcm16",
                "dual_window": True,
                "interval": 0.25,
                "fast_window": 2.0,
                "quality_window": 4.0,
                "quality_interval": 0.5,
                "quality_holdback": 0.2,
                "holdback": 0.2,
                **overrides,
            }
        )
    )
    return json.loads(await socket.recv())


async def test_two_windows_preview_before_commit_and_bounded_overlap_without_omission():
    recognizer = Recognizer()
    service = DictationServer(Settings(), recognizer=recognizer, detector_factory=Detector)
    audio = 0.1 + np.arange(10 * RATE, dtype=np.float32) / RATE * 0.01
    try:
        async with serve(service.handle, "127.0.0.1", 0) as listener:
            port = listener.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}/v1/dictate", proxy=None) as socket:
                assert (await start(socket))["dual_window"] is True
                receive = asyncio.create_task(collect(socket))
                for offset in range(0, len(audio), 1600):
                    await socket.send(pcm(audio[offset : offset + 1600]))
                    await asyncio.sleep(0.025)
                await socket.send('{"type":"stop"}')
                events = await asyncio.wait_for(receive, 4)
        assert events[-1]["type"] == "session_end", events[-1]
        assert events[-1]["text"] == " ".join(f"слово{i}" for i in range(10))
        commits = [event for event in events if event["type"] == "commit"]
        previews = [event for event in events if event["type"] == "partial" and event["source"] == "fast"]
        assert previews and all(e["audio_end"] - e["audio_start"] <= 2.001 for e in previews)
        assert events.index(previews[0]) < events.index(commits[0])
        assert any(
            e["type"] == "partial" and e["source"] == "quality" and e["audio_end"] - e["audio_start"] > 2
            for e in events
        )
        assert max(recognizer.durations) <= 4.04
        assert all(e["source"] == "quality" for e in commits)
        assert "".join(e["delta"] for e in commits) == events[-1]["text"]
        assert [e["seq"] for e in commits] == list(range(1, len(commits) + 1))
        assert events[-1]["dual_window"]["fast_decodes"] > 0
        assert events[-1]["dual_window"]["quality_decodes"] > 0
    finally:
        service.pool.shutdown(wait=True)


async def test_stop_during_quality_pass_flushes_audio_captured_in_parallel():
    entered, release = threading.Event(), threading.Event()
    recognizer = Recognizer()
    calls = 0

    service = DictationServer(Settings(), recognizer=recognizer, detector_factory=Detector)
    original = recognizer.decode

    def decode(audio):
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            assert release.wait(3)
        return original(audio)

    recognizer.decode = decode
    try:
        async with serve(service.handle, "127.0.0.1", 0) as listener:
            port = listener.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}", proxy=None) as socket:
                await start(socket)
                await socket.send(pcm(np.full(RATE, 0.1)))
                await socket.send(pcm(np.full(1600, 0.1)))
                assert await asyncio.to_thread(entered.wait, 2)
                await socket.send(pcm(np.full(RATE, 0.1)))
                await socket.send('{"type":"stop"}')
                await asyncio.sleep(0.05)
                release.set()
                events = await asyncio.wait_for(collect(socket), 3)
        assert events[-1]["type"] == "session_end"
        assert events[-1]["audio_seconds"] == 2.1
        assert events[-1]["text"] == "слово0 слово1"
        assert sum(e["type"] == "segment_end" for e in events) == 1
    finally:
        release.set()
        service.pool.shutdown(wait=True)


@pytest.mark.parametrize("options", [{"dual_window": "true"}, {"quality_window": 100}])
async def test_invalid_dual_parameters_are_refused(options):
    service = DictationServer(Settings(), recognizer=Recognizer(), detector_factory=Detector)
    try:
        async with serve(service.handle, "127.0.0.1", 0) as listener:
            port = listener.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}", proxy=None) as socket:
                assert (await start(socket, **options))["type"] == "error"
    finally:
        service.pool.shutdown(wait=True)


async def test_cancel_while_decoding_releases_session_without_waiting_for_native_pass():
    entered, release = threading.Event(), threading.Event()
    recognizer = Recognizer()
    original = recognizer.decode

    def blocking(audio):
        entered.set()
        assert release.wait(3)
        return original(audio)

    recognizer.decode = blocking
    service = DictationServer(Settings(), recognizer=recognizer, detector_factory=Detector)
    try:
        async with serve(service.handle, "127.0.0.1", 0) as listener:
            port = listener.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}", proxy=None) as socket:
                await start(socket)
                await socket.send(pcm(np.full(RATE, 0.1)))
                await socket.send(pcm(np.full(1600, 0.1)))
                assert await asyncio.to_thread(entered.wait, 2)
                await socket.send('{"type":"cancel"}')
                events = await asyncio.wait_for(collect(socket), 0.5)
                assert events[-1]["type"] == "cancelled"
                assert events[-1]["text"] == ""
            await asyncio.sleep(0.05)
            assert not service.active
    finally:
        release.set()
        service.pool.shutdown(wait=True)
