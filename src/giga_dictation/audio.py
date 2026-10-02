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


@dataclass(eq=False)
class Segment:
    start: int
    frames: deque = field(default_factory=deque)
    count: int = 0
    closed: bool = False
    forced: bool = False
    decoded_end: int = -1

    @property
    def end(self) -> int:
        return self.start + self.count

    def append(self, frame: np.ndarray) -> None:
        self.frames.append(frame.copy())
        self.count += len(frame)

    def snapshot(self) -> tuple[np.ndarray, float]:
        return np.concatenate(self.frames), self.start / RATE

    def trim(self, before: int) -> None:
        remaining = min(max(0, before - self.start), self.count)
        while remaining and self.frames:
            frame = self.frames[0]
            taken = min(remaining, len(frame))
            if taken == len(frame):
                self.frames.popleft()
            else:
                self.frames[0] = frame[taken:]
            self.start += taken
            self.count -= taken
            remaining -= taken


class Segmenter:
    """Frame-aligned neural VAD; preserve pre-roll, stop tail and forced-cut overlap."""

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
        if self.buffered_seconds > 60:
            raise ValueError("Сервер отстаёт более чем на 60 секунд. Завершите диктовку и уменьшите нагрузку")

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
        if self.silent_frames * 512 / RATE >= self.settings.silence:
            self._close()
        elif self.current.count / RATE >= self.settings.window:
            old = self.current
            old.closed = old.forced = True
            self.finished.append(old)
            snapshot, _ = old.snapshot()
            # Keep the whole unconfirmed tail even when holdback is configured above
            # its default. Otherwise a larger holdback can drop speech at a forced cut.
            overlap_seconds = max(1.5, self.settings.holdback + 0.5)
            overlap = snapshot[-int(overlap_seconds * RATE) :]
            self.current = Segment(old.end - len(overlap))
            self.current.append(overlap)

    def _close(self) -> None:
        if self.current:
            self.current.closed = True
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
