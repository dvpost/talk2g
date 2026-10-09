"""Real-time continuous speech: bounded windows, ordered commits and stop latency."""

import asyncio
import json
import time
from pathlib import Path

import numpy as np
from websockets.asyncio.client import connect

from talk2g.audio import pcm, read_audio
from talk2g.config import RATE


async def main():
    audio = np.tile(read_audio("tests/fixtures/example.wav"), 4)
    events = []
    began = time.monotonic()
    stopped = 0
    async with connect("ws://127.0.0.1:8769/v1/dictate", proxy=None) as socket:
        await socket.send(
            json.dumps(
                {
                    "type": "start",
                    "version": 1,
                    "rate": RATE,
                    "format": "pcm16",
                    "window": 4,
                    "recognize_on_pause": False,
                }
            )
        )
        assert json.loads(await socket.recv())["type"] == "ready"

        async def sending():
            nonlocal stopped
            for offset in range(0, len(audio), 1600):
                await socket.send(pcm(audio[offset : offset + 1600]))
                await asyncio.sleep(max(0, began + min(offset + 1600, len(audio)) / RATE - time.monotonic()))
            stopped = time.monotonic()
            await socket.send('{"type":"stop"}')

        task = asyncio.create_task(sending())
        try:
            async for message in socket:
                event = json.loads(message)
                event["received_seconds"] = time.monotonic() - began
                events.append(event)
                if event["type"] == "error":
                    raise RuntimeError(event["message"])
                if event["type"] == "session_end":
                    break
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    commits = [e for e in events if e["type"] == "commit"]
    final = events[-1]
    assert final["type"] == "session_end"
    assert "".join(e["delta"] for e in commits) == final["text"]
    assert [e["seq"] for e in commits] == list(range(1, len(commits) + 1))
    assert final["text"].lower().count("лукоморья") == 4, final["text"]
    maximum_buffer = max(e["buffer_seconds"] for e in events if e["type"] == "partial")
    assert maximum_buffer < 7, maximum_buffer
    report = {
        "audio_seconds": len(audio) / RATE,
        "first_commit_seconds": commits[0]["received_seconds"],
        "stop_latency_seconds": time.monotonic() - stopped,
        "max_buffer_seconds": maximum_buffer,
        "max_decode_seconds": max(e["decode_seconds"] for e in commits),
        "text": final["text"],
        "commits": len(commits),
        "events": events,
    }
    Path("benchmarks/long-stream-results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in report.items() if k != "events"}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
