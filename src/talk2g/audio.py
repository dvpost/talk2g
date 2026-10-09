from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from math import gcd
from pathlib import Path
from typing import Protocol

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

from .config import RATE, Settings


def read_audio(path: str | Path) -> np.ndarray:
    audio, rate = sf.read(path, dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)
    if rate != RATE:
        divisor = gcd(rate, RATE)
        audio = resample_poly(audio, RATE // divisor, rate // divisor).astype(np.float32)
    if not np.isfinite(audio).all():
        raise ValueError("В записи есть недопустимые аудиосэмплы")
    return np.clip(audio, -1, 1)


def pcm(audio: np.ndarray) -> bytes:
    return (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()


class Detector(Protocol):
    def probability(self, frame: np.ndarray) -> float: ...


class SpeechTimeout:
    """Track captured speech time independently of recognition and model loading."""

    def __init__(self, detector: Detector, seconds: float, started_at: float, *, release_after: float = 0.6):
        self.detector = detector
        self.seconds = seconds
        self.last_speech = started_at
        self.pending = np.empty(0, dtype=np.float32)
        self.voice_frames = 0
        self.speech_seen = False
        self.release_after = release_after
        self.speech_open = False
        self.qualified_speech = False
        self.silent_frames = 0

    def feed(self, data: bytes, captured_at: float) -> None:
        samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768
        self.pending = np.concatenate((self.pending, samples))
        while len(self.pending) >= 512:
            frame, self.pending = self.pending[:512], self.pending[512:]
            threshold = 0.35 if self.speech_open else 0.5
            voiced = self.detector.probability(frame) >= threshold
            self.voice_frames = self.voice_frames + 1 if voiced else 0
            self.silent_frames = 0 if voiced else self.silent_frames + 1
            if voiced:
                self.speech_open = True
            elif self.silent_frames * 512 / RATE >= self.release_after:
                self.speech_open = self.qualified_speech = False
            # Ignore isolated clicks, as the server's segmenter does.
            if voiced and (self.voice_frames >= 3 or self.qualified_speech):
                self.qualified_speech = True
                self.speech_seen = True
                self.last_speech = max(self.last_speech, captured_at - len(self.pending) / RATE)

    def remaining(self, now: float, enabled_at: float = 0.0) -> float:
        return max(0.0, min(self.seconds, self.seconds - (now - max(self.last_speech, enabled_at))))


@dataclass(eq=False)
class Segment:
    start: int
    frames: deque = field(default_factory=deque)
    count: int = 0

    def append(self, frame: np.ndarray) -> None:
        self.frames.append(frame.copy())
        self.count += len(frame)

    def snapshot(self) -> tuple[np.ndarray, float]:
        return np.concatenate(self.frames), self.start / RATE

    def trim_tail(self, samples: int) -> None:
        remaining = min(samples, self.count)
        while remaining and self.frames:
            frame = self.frames.pop()
            taken = min(remaining, len(frame))
            if taken < len(frame):
                self.frames.append(frame[:-taken])
            self.count -= taken
            remaining -= taken


class Segmenter:
    """Frame-aligned neural VAD; submit whole blocks after silence or Stop."""

    def __init__(self, detector: Detector, settings: Settings):
        self.detector = detector
        self.settings = settings
        self.pending = np.empty(0, dtype=np.float32)
        self.position = 0
        self.pre_roll: deque[np.ndarray] = deque(maxlen=10)
        self.current: Segment | None = None
        self.finished: deque[Segment] = deque()
        self.silent_frames = 0
        self.voice_frames = 0

    def feed(self, data: bytes) -> None:
        if not data or len(data) % 2:
            raise ValueError("Аудиопакет должен содержать целые PCM16 сэмплы")
        samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768
        self.pending = np.concatenate((self.pending, samples))
        while len(self.pending) >= 512:
            frame, self.pending = self.pending[:512], self.pending[512:]
            self._frame(frame)
        if self.buffered_seconds > 300:
            raise ValueError("Накоплено более 300 секунд речи. Сделайте паузу или завершите диктовку")

    @property
    def buffered_seconds(self) -> float:
        return (
            sum(item.count for item in self.finished) + (self.current.count if self.current else 0)
        ) / RATE

    def _frame(self, frame: np.ndarray) -> None:
        probability = self.detector.probability(frame)
        self.position += len(frame)
        voiced = probability >= (0.35 if self.current else 0.5)
        if self.current is None:
            self.pre_roll.append(frame)
            if not voiced:
                return
            frames = list(self.pre_roll)
            self.current = Segment(self.position - sum(map(len, frames)))
            for old in frames:
                self.current.append(old)
            self.pre_roll.clear()
            self.voice_frames = 1
            self.silent_frames = 0
            return
        self.current.append(frame)
        self.silent_frames = 0 if voiced else self.silent_frames + 1
        self.voice_frames += int(voiced)
        if self.silent_frames * 512 / RATE >= self.settings.recognition_pause:
            # Keep a short acoustic tail; the archive retains every accepted PCM sample.
            self.current.trim_tail(max(0, self.silent_frames * 512 - int(0.3 * RATE)))
            self._close()

    def _close(self) -> None:
        if self.current:
            if self.voice_frames >= 3:
                self.finished.append(self.current)
            self.current = None
        self.silent_frames = 0
        self.voice_frames = 0

    def stop(self) -> None:
        # Preserve every captured sample, including a sub-512 stop tail.
        if len(self.pending):
            if self.current:
                self.current.append(self.pending)
            else:
                padded = np.pad(self.pending, (0, 512 - len(self.pending)))
                if self.detector.probability(padded) >= 0.5:
                    self.current = Segment(self.position - sum(map(len, self.pre_roll)))
                    for frame in self.pre_roll:
                        self.current.append(frame)
                    self.current.append(self.pending)
                    self.voice_frames = 3
            self.position += len(self.pending)
            self.pending = np.empty(0, dtype=np.float32)
        self._close()
