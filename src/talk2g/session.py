"""One dictation's audio, segmentation, transcript and decode scheduling."""

import asyncio
import time
from collections.abc import Callable
from concurrent.futures import Executor

from .audio import Detector, Segmenter
from .config import RATE, Settings
from .model import Word
from .session_output import SessionOutput
from .transcript import Transcript


class RecognitionSession:
    def __init__(
        self,
        settings: Settings,
        detector: Detector,
        recognizer,
        pool: Executor,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.recognizer = recognizer
        self.pool = pool
        self.clock = clock
        self.segmenter = Segmenter(detector, settings)
        self.transcript = Transcript()
        self.changed = asyncio.Event()
        self.stopped = False
        self.sent_bytes = 0
        self.started_at = self.clock()

    @property
    def text(self) -> str:
        return self.transcript.text

    def feed(self, packet: bytes):
        if len(packet) > 2 * RATE:
            raise ValueError("Аудиопакет не должен превышать одну секунду")
        self.sent_bytes += len(packet)
        if self.sent_bytes > 2 * RATE * 7200:
            raise ValueError("Максимальная продолжительность сессии — два часа")
        self.segmenter.feed(packet)
        self.changed.set()

    def stop(self):
        self.segmenter.stop()
        self.stopped = True
        self.changed.set()

    async def run(self, output: SessionOutput):
        while True:
            if not self.segmenter.finished:
                if self.stopped:
                    await output.send(
                        {
                            "type": "session_end",
                            "text": self.transcript.text,
                            "audio_seconds": self.sent_bytes / (2 * RATE),
                            "elapsed_seconds": self.clock() - self.started_at,
                        }
                    )
                    return
                self.changed.clear()
                await self.changed.wait()
                continue
            segment = self.segmenter.finished.popleft()
            audio, offset = segment.snapshot()
            snapshot_end = offset + len(audio) / RATE
            await output.send(
                {
                    "type": "recognizing",
                    "audio_start": offset,
                    "audio_end": snapshot_end,
                    "capturing": not self.stopped,
                }
            )
            decode_index = output.decode_started(offset, len(audio))
            decode_start = self.clock()
            words = await asyncio.get_running_loop().run_in_executor(self.pool, self.recognizer.decode, audio)
            words = [Word(w.text, w.start + offset, w.end + offset) for w in words]
            delta = self.transcript.commit_block(words)
            duration = self.clock() - decode_start
            output.decode_finished(
                {
                    "type": "decode_end",
                    "index": decode_index,
                    "words": [{"text": w.text, "start": w.start, "end": w.end} for w in words],
                    "delta": delta,
                    "final": True,
                    "decode_seconds": duration,
                }
            )
            if delta:
                await output.send(
                    {
                        "type": "commit",
                        "seq": self.transcript.sequence,
                        "delta": delta,
                        "audio_end": self.transcript.frontier,
                        "decode_seconds": duration,
                    }
                )
            await output.send({"type": "segment_end"})
