"""Real-time speech blocks: recognition after pauses, ordered commits and Stop latency."""

import asyncio
import json
import time
from pathlib import Path

import numpy as np
from websockets.asyncio.client import connect

from talk2g.audio import pcm, read_audio
from talk2g.config import RATE


async def main():
    phrase = read_audio("tests/fixtures/example.wav")
    audio = np.concatenate([np.tile(np.concatenate([phrase, np.zeros(int(3.5 * RATE))]), 3), phrase])
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
                    "recognition_pause": 3,
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
    maximum_block = max(e["audio_end"] - e["audio_start"] for e in events if e["type"] == "recognizing")
    assert maximum_block < len(phrase) / RATE + 1, maximum_block
    assert len(commits) == 4
    report = {
        "audio_seconds": len(audio) / RATE,
        "first_commit_seconds": commits[0]["received_seconds"],
        "stop_latency_seconds": time.monotonic() - stopped,
        "max_block_seconds": maximum_block,
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
