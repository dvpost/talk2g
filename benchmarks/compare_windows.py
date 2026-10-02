"""Replay identical audio through baseline/dual windows, reporting actual WER and latency."""

import argparse
import asyncio
import json
import re
import time
from pathlib import Path

import numpy as np
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from giga_dictation.audio import pcm, read_audio
from giga_dictation.config import RATE, Settings
from giga_dictation.server import DictationServer


def words(text, aliases=None):
    text = text.lower().replace("ё", "е")
    for value, replacement in (aliases or {}).items():
        text = re.sub(r"\b" + re.escape(value) + r"\b", replacement, text)
    return re.findall(r"\w+", text)


def distance(reference, hypothesis):
    row = list(range(len(hypothesis) + 1))
    for i, expected in enumerate(reference, 1):
        new = [i]
        for j, actual in enumerate(hypothesis, 1):
            new.append(min(new[-1] + 1, row[j] + 1, row[j - 1] + (expected != actual)))
        row = new
    return row[-1]


class MeasuredRecognizer:
    def __init__(self, delegate):
        self.delegate = delegate
        self.vad_path = delegate.vad_path
        self.calls = 0
        self.seconds = 0.0

    def decode(self, audio):
        began = time.monotonic()
        try:
            return self.delegate.decode(audio)
        finally:
            self.calls += 1
            self.seconds += time.monotonic() - began


async def replay(url, audio, dual, recognizer, case, overrides=None):
    recognizer.calls = 0
    recognizer.seconds = 0
    events = []
    first_preview = first_commit = stop_at = None
    lags = []
    began = time.monotonic()
    async with connect(url, proxy=None, compression=None) as socket:
        await socket.send(
            json.dumps(
                {
                    "type": "start",
                    "version": 1,
                    "rate": RATE,
                    "format": "pcm16",
                    "dual_window": dual,
                    **(overrides or {}),
                }
            )
        )
        ready = json.loads(await socket.recv())
        assert ready["type"] == "ready" and ready["dual_window"] == dual, ready
        began = time.monotonic()

        async def send_audio():
            nonlocal stop_at
            for offset in range(0, len(audio), 1600):
                await socket.send(pcm(audio[offset : offset + 1600]))
                await asyncio.sleep(max(0, began + min(offset + 1600, len(audio)) / RATE - time.monotonic()))
            stop_at = time.monotonic()
            await socket.send('{"type":"stop"}')

        sender = asyncio.create_task(send_audio())
        try:
            async with asyncio.timeout(len(audio) / RATE + 25):
                async for message in socket:
                    event = json.loads(message)
                    now = time.monotonic() - began
                    assert event["type"] != "error", event
                    events.append(event)
                    if event["type"] == "partial" and event["text"] and first_preview is None:
                        first_preview = now
                    if event["type"] == "commit":
                        first_commit = now if first_commit is None else first_commit
                        lags.append(now - event["audio_end"])
                    if event["type"] == "session_end":
                        break
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
    final = events[-1]
    commits = [e for e in events if e["type"] == "commit"]
    assert final["type"] == "session_end" and stop_at is not None
    assert "".join(e["delta"] for e in commits) == final["text"]
    assert [e["seq"] for e in commits] == list(range(1, len(commits) + 1))
    expected = words(case["reference"], case.get("aliases"))
    actual = words(final["text"], case.get("aliases"))
    errors = distance(expected, actual)
    return {
        "case": case["name"],
        "mode": "dual" if dual else "baseline",
        "audio_seconds": len(audio) / RATE,
        "reference": case["reference"],
        "text": final["text"],
        "reference_words": len(expected),
        "word_errors": errors,
        "wer": errors / len(expected),
        "cer": distance(" ".join(expected), " ".join(actual)) / len(" ".join(expected)),
        "first_preview_seconds": first_preview,
        "first_commit_seconds": first_commit,
        "first_commit_before_stop": first_commit is not None and began + first_commit < stop_at,
        "stop_to_final_seconds": time.monotonic() - stop_at,
        "commit_lag_p95_seconds": float(np.percentile(lags, 95)) if lags else None,
        "decode_calls": recognizer.calls,
        "decode_seconds": recognizer.seconds,
        "inference_busy_fraction": recognizer.seconds / (time.monotonic() - began),
        "max_buffer_seconds": max((e.get("buffer_seconds", 0) for e in events), default=0),
        "windows": final.get("dual_window"),
    }


async def main(args):
    root = Path(__file__).resolve().parents[1]
    cases = json.loads(args.cases.read_text())
    if args.case:
        cases = [case for case in cases if case["name"] in args.case]
        if not cases:
            raise ValueError("No selected benchmark cases")
    settings = Settings()
    service = DictationServer(settings)
    await service.load()
    if service.loading_error:
        raise RuntimeError(service.loading_error)
    measured = MeasuredRecognizer(service.recognizer)
    service.recognizer = measured
    rows = []
    try:
        async with serve(service.handle, "127.0.0.1", 0) as listener:
            port = listener.sockets[0].getsockname()[1]
            for case in cases:
                audio = read_audio(root / case["audio"])
                for dual in (False, True):
                    overrides = {"silence": args.silence} if args.silence is not None else {}
                    row = await replay(
                        f"ws://127.0.0.1:{port}/v1/dictate", audio, dual, measured, case, overrides
                    )
                    rows.append(row)
                    print(json.dumps(row, ensure_ascii=False), flush=True)
        aggregate = {}
        for mode in ("baseline", "dual"):
            selected = [row for row in rows if row["mode"] == mode]
            errors = sum(row["word_errors"] for row in selected)
            count = sum(row["reference_words"] for row in selected)
            aggregate[mode] = {
                "word_errors": errors,
                "reference_words": count,
                "wer": errors / count,
                "decode_seconds": sum(row["decode_seconds"] for row in selected),
                "first_commit_seconds_median": float(
                    np.median([row["first_commit_seconds"] for row in selected])
                ),
                "stop_to_final_seconds_max": max(row["stop_to_final_seconds"] for row in selected),
            }
        result = {
            "settings": {
                "fast_window": settings.fast_window,
                "quality_window": settings.quality_window,
                "interval": settings.interval,
                "quality_interval": settings.quality_interval,
                "quality_holdback": settings.quality_holdback,
            },
            "note": "Small public/synthetic test set; not an accuracy guarantee for the user's voice.",
            "overrides": {"silence": args.silence} if args.silence is not None else {},
            "aggregate": aggregate,
            "cases": rows,
        }
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(aggregate), flush=True)
    finally:
        service.pool.shutdown(wait=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=Path("tests/fixtures/dual-window-cases.json"))
    parser.add_argument("--output", type=Path, default=Path("benchmarks/dual-window-comparison.json"))
    parser.add_argument("--case", action="append", help="Run only selected case names")
    parser.add_argument("--silence", type=float, help="Override pause detection for continuous-speech stress")
    asyncio.run(main(parser.parse_args()))
