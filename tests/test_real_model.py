"""Optional real speech regression: no stubs between PCM and ordered text events."""

import asyncio
import json
import os
import wave
from pathlib import Path

import numpy as np
import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from talk2g.audio import SpeechTimeout, pcm, read_audio
from talk2g.config import RATE, Settings
from talk2g.model import SileroDetector, prepare_vad
from talk2g.server import DictationServer


@pytest.mark.real_model
@pytest.mark.skipif(not os.environ.get("TALK2G_REAL_MODEL_TESTS"), reason="Set TALK2G_REAL_MODEL_TESTS=1")
def test_real_silero_resets_timeout_on_speech_and_expires_after_silence():
    timeout = SpeechTimeout(SileroDetector(prepare_vad()), 45, started_at=0)
    timeout.feed(pcm(np.zeros(RATE)), captured_at=1)
    assert timeout.remaining(1) == 44
    audio = read_audio(Path(__file__).parent / "fixtures/example.wav")
    resets = 0
    for offset in range(0, len(audio), 1600):
        before = timeout.last_speech
        captured_at = 1 + min(offset + 1600, len(audio)) / RATE
        timeout.feed(pcm(audio[offset : offset + 1600]), captured_at)
        resets += timeout.last_speech > before
    assert resets > 10 and timeout.last_speech > 1
    # Allow the detector's short speech tail to settle, then check pure silence.
    timeout.feed(pcm(np.zeros(RATE)), captured_at=captured_at + 1)
    last_speech = timeout.last_speech
    timeout.feed(pcm(np.zeros(5 * RATE)), captured_at=captured_at + 6)
    assert timeout.last_speech == last_speech
    assert timeout.remaining(last_speech + 44) == 1
    assert timeout.remaining(last_speech + 45) == 0


@pytest.mark.real_model
@pytest.mark.skipif(not os.environ.get("TALK2G_REAL_MODEL_TESTS"), reason="Set TALK2G_REAL_MODEL_TESTS=1")
async def test_real_gigaam_recognizes_whole_blocks_after_pause_and_on_stop(tmp_path):
    service = DictationServer(Settings(recognition_pause=3))
    await service.load()
    assert not service.loading_error
    service.home = tmp_path
    audio = read_audio(Path(__file__).parent / "fixtures/example.wav")
    events, inputs = [], []
    original = service.recognizer.decode

    def decode(block):
        inputs.append(block.copy())
        return original(block)

    service.recognizer.decode = decode
    try:
        async with serve(service.handle, "127.0.0.1", 0) as listener:
            port = listener.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}", proxy=None) as socket:
                await socket.send(
                    json.dumps(
                        {
                            "type": "start",
                            "version": 1,
                            "rate": RATE,
                            "format": "pcm16",
                            "recognize_on_pause": True,
                            "recognition_pause": 3,
                            "save_recordings": True,
                        }
                    )
                )
                ready = json.loads(await socket.recv())
                assert ready["recognize_on_pause"] and ready["recognition_pause"] == 3
                for offset in range(0, len(audio), RATE):
                    await socket.send(pcm(audio[offset : offset + RATE]))
                await asyncio.sleep(0.4)
                assert not inputs  # no live inference, even beyond the old window length
                for _ in range(4):
                    await socket.send(pcm(np.zeros(RATE)))
                async with asyncio.timeout(15):
                    while True:
                        event = json.loads(await socket.recv())
                        events.append(event)
                        assert event["type"] != "error", event
                        if event["type"] == "segment_end":
                            break
                assert len(inputs) == 1
                # A second block is finalized immediately by Stop, without another pause.
                for offset in range(0, len(audio), RATE):
                    await socket.send(pcm(audio[offset : offset + RATE]))
                await socket.send('{"type":"stop"}')
                async with asyncio.timeout(15):
                    while True:
                        event = json.loads(await socket.recv())
                        events.append(event)
                        assert event["type"] != "error", event
                        if event["type"] == "session_end":
                            break
        commits = [event for event in events if event["type"] == "commit"]
        assert len(inputs) == len(commits) == 2
        assert all("лукоморья" in event["delta"].lower() for event in commits)
        assert not any(event["type"] == "partial" for event in events)
        assert "".join(event["delta"] for event in commits) == events[-1]["text"]
        assert [event["seq"] for event in commits] == [1, 2]
        directory = Path(events[-1]["recording_path"])
        with wave.open(str(directory / "audio.wav")) as recording:
            received = recording.readframes(recording.getnframes())
        assert received == pcm(audio) + pcm(np.zeros(4 * RATE)) + pcm(audio)
        info = json.loads((directory / "session.json").read_text(encoding="utf-8"))
        assert info["status"] == "completed" and info["decode_count"] == 2
    finally:
        service.pool.shutdown(wait=True, cancel_futures=True)
