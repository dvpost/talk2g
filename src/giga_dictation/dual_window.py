from __future__ import annotations

import asyncio
import time
from collections import deque

from .config import RATE
from .model import Word
from .transcript import Transcript, join_words, key


class WindowAgreement:
    """Align quality words with the latest short-window hypothesis covering their time."""

    def __init__(self, holdback):
        self.transcript = Transcript(holdback)
        self.fast = deque(maxlen=64)
        self.disagreements = 0

    def preview(self, words, start, end):
        self.fast.append((start, end, words))
        return join_words(self.transcript.remaining(words))

    def quality(self, words, end, *, final=False, forced=False):
        remaining = self.transcript.remaining(words)
        agreed = []
        for word, closest in zip(remaining, self.aligned(remaining), strict=True):
            if closest is None or key(closest.text) != key(word.text):
                self.disagreements += 1
                break
            agreed.append(closest)
        self.transcript.previous = agreed
        return self.transcript.update(words, end, final=final, forced=forced)

    def aligned(self, words, snapshots=None):
        aligned = []
        previous_end = -1.0
        for word in words:
            snapshot = next(
                (
                    items
                    for start, stop, items in reversed(self.fast if snapshots is None else snapshots)
                    if word.start >= start - 0.05 and word.end <= stop + 0.05
                ),
                [],
            )
            candidates = [
                other
                for other in snapshot
                if abs(other.start - word.start) <= 0.45 and other.end > previous_end + 0.01
            ]
            closest = min(candidates, key=lambda other: abs(other.start - word.start)) if candidates else None
            aligned.append(closest)
            if closest is not None:
                previous_end = closest.end
        return aligned


class DualWindowProcessor:
    """One model/worker, two audio contexts; no inference request backlog or editor rewrites."""

    def __init__(
        self, settings, segmenter, decode, send, changed, stopped, sent_bytes, started_at, rescore=None
    ):
        self.settings = settings
        self.segmenter = segmenter
        self.decode = decode
        self.send = send
        self.changed = changed
        self.stopped = stopped
        self.sent_bytes = sent_bytes
        self.started_at = started_at
        self.rescore = rescore
        self.agreement = WindowAgreement(max(settings.holdback, settings.quality_holdback))
        self.transcript = self.agreement.transcript
        self.metrics = {
            "fast_decodes": 0,
            "quality_decodes": 0,
            "decode_seconds": 0.0,
            "max_buffer_seconds": 0.0,
        }
        if rescore is not None:
            self.metrics["language_model"] = {
                "comparisons": 0,
                "short_choices": 0,
                "long_choices": 0,
                "abstentions": 0,
                "seconds": 0.0,
            }

    async def recognize(self, audio, offset, kind):
        began = time.monotonic()
        words = await self.decode(audio)
        duration = time.monotonic() - began
        self.metrics["max_buffer_seconds"] = max(
            self.metrics["max_buffer_seconds"], self.segmenter.buffered_seconds
        )
        self.metrics[kind + "_decodes"] += 1
        self.metrics["decode_seconds"] += duration
        return [Word(w.text, w.start + offset, w.end + offset) for w in words], duration

    async def wait(self, timeout=None):
        self.changed.clear()
        if timeout is None:
            await self.changed.wait()
        else:
            try:
                await asyncio.wait_for(self.changed.wait(), max(0.01, timeout))
            except TimeoutError:
                pass

    async def run(self):
        segment_before = None
        fast_end = quality_end = -1
        fast_at = quality_at = 0.0
        while True:
            self.metrics["max_buffer_seconds"] = max(
                self.metrics["max_buffer_seconds"], self.segmenter.buffered_seconds
            )
            segment = self.segmenter.finished[0] if self.segmenter.finished else self.segmenter.current
            if segment is None:
                if self.stopped():
                    self.metrics["disagreements"] = self.agreement.disagreements
                    await self.send(
                        {
                            "type": "session_end",
                            "text": self.transcript.text,
                            "audio_seconds": self.sent_bytes() / (2 * RATE),
                            "elapsed_seconds": time.monotonic() - self.started_at,
                            "dual_window": self.metrics,
                        }
                    )
                    return
                await self.wait()
                continue
            if segment is not segment_before:
                segment_before = segment
                fast_end = quality_end = -1
            if not segment.closed and segment.count < RATE:
                await self.wait()
                continue
            if (
                (not segment.closed or self.rescore is not None)
                and segment.end != fast_end
                and (segment.closed or time.monotonic() - fast_at >= self.settings.interval)
            ):
                audio, offset = segment.snapshot()
                audio_end = offset + len(audio) / RATE
                size = round(self.settings.fast_window * RATE)
                short = audio[-size:]
                short_offset = audio_end - len(short) / RATE
                words, duration = await self.recognize(short, short_offset, "fast")
                preview = self.agreement.preview(words, short_offset, audio_end)
                await self.send(
                    {
                        "type": "partial",
                        "text": preview,
                        "source": "fast",
                        "audio_start": short_offset,
                        "audio_end": audio_end,
                        "decode_seconds": duration,
                        "buffer_seconds": self.segmenter.buffered_seconds,
                    }
                )
                fast_end = round(audio_end * RATE)
                fast_at = time.monotonic()
            if segment.closed or (
                segment.end != quality_end and time.monotonic() - quality_at >= self.settings.quality_interval
            ):
                audio, offset = segment.snapshot()
                audio_end = offset + len(audio) / RATE
                words, duration = await self.recognize(audio, offset, "quality")
                quality_end = round(audio_end * RATE)
                final = segment.closed and quality_end == segment.end
                if self.rescore is not None:
                    selection = await self.rescore(self.agreement, words, audio_end)
                    details = selection.details
                    if details["compared"]:
                        metrics = self.metrics["language_model"]
                        metrics["comparisons"] += 1
                        metrics["seconds"] += details["seconds"]
                        category = {
                            "short": "short_choices",
                            "long": "long_choices",
                            "abstain": "abstentions",
                        }
                        metrics[category[details["choice"]]] += 1
                        await self.send({"type": "rescore", **details})
                    self.transcript.previous = selection.confirmed
                    delta, partial = self.transcript.update(
                        selection.words, audio_end, final=final, forced=segment.forced
                    )
                else:
                    delta, partial = self.agreement.quality(
                        words, audio_end, final=final, forced=segment.forced
                    )
                if delta:
                    await self.send(
                        {
                            "type": "commit",
                            "seq": self.transcript.sequence,
                            "delta": delta,
                            "source": "quality",
                            "audio_end": self.transcript.frontier,
                            "decode_seconds": duration,
                        }
                    )
                await self.send(
                    {
                        "type": "partial",
                        "text": partial,
                        "source": "quality",
                        "audio_start": offset,
                        "audio_end": audio_end,
                        "decode_seconds": duration,
                        "buffer_seconds": self.segmenter.buffered_seconds,
                    }
                )
                quality_at = time.monotonic()
                if final:
                    if segment in self.segmenter.finished:
                        self.segmenter.finished.remove(segment)
                    if not segment.forced:
                        await self.send({"type": "segment_end"})
                    continue
            if self.segmenter.finished:
                continue
            if fast_end == segment.end and quality_end == segment.end:
                await self.wait()
            else:
                deadlines = []
                if fast_end != segment.end:
                    deadlines.append(fast_at + self.settings.interval)
                if quality_end != segment.end:
                    deadlines.append(quality_at + self.settings.quality_interval)
                await self.wait(min(deadlines) - time.monotonic())
