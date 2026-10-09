"""Optional real speech regression: no stubs between PCM and ordered text events."""

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from talk2g.audio import pcm, read_audio
from talk2g.config import RATE, Settings
from talk2g.server import DictationServer


@pytest.mark.real_model
@pytest.mark.skipif(not os.environ.get("TALK2G_REAL_MODEL_TESTS"), reason="Set TALK2G_REAL_MODEL_TESTS=1")
async def test_real_gigaam_commits_while_audio_is_being_sent_and_flushes_stop():
    service = DictationServer(Settings())
    await service.load()
    assert not service.loading_error
    audio = read_audio(Path(__file__).parent / "fixtures/example.wav")
    events = []
    sent_stop = False
    progressive = False
    started = time.monotonic()
    try:
        async with serve(service.handle, "127.0.0.1", 0) as listener:
            port = listener.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}", proxy=None) as socket:
                await socket.send(
                    json.dumps({"type": "start", "version": 1, "rate": RATE, "format": "pcm16"})
                )
                assert json.loads(await socket.recv())["type"] == "ready"

                async def sending():
                    nonlocal sent_stop
                    for offset in range(0, len(audio), 1600):
                        await socket.send(pcm(audio[offset : offset + 1600]))
                        await asyncio.sleep(
                            max(0, started + min(offset + 1600, len(audio)) / RATE - time.monotonic())
                        )
                    sent_stop = True
                    await socket.send('{"type":"stop"}')

                sender = asyncio.create_task(sending())
                try:
                    async with asyncio.timeout(30):
                        async for message in socket:
                            event = json.loads(message)
                            events.append(event)
                            assert event["type"] != "error", event
                            if event["type"] == "commit" and not sent_stop:
                                progressive = True
                            if event["type"] == "session_end":
                                break
                finally:
                    sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)
        commits = [event for event in events if event["type"] == "commit"]
        assert progressive
        assert events[-1]["type"] == "session_end"
        assert "лукоморья" in events[-1]["text"].lower()
        assert "мои" in events[-1]["text"].lower()
        assert "".join(event["delta"] for event in commits) == events[-1]["text"]
        assert [event["seq"] for event in commits] == list(range(1, len(commits) + 1))
        assert time.monotonic() - started < len(audio) / RATE + 5
    finally:
        service.pool.shutdown(wait=True, cancel_futures=True)
