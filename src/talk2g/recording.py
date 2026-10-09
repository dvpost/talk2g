from __future__ import annotations

import json
import logging
import wave
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .config import RATE, Settings, project_home

log = logging.getLogger(__name__)


def recordings_directory(home: Path | None = None) -> Path:
    return (home or project_home()) / "recordings"


class SessionRecording:
    """Save accepted PCM and the exact sample ranges submitted to recognition."""

    def __init__(self, settings: Settings, session_id, home: Path | None = None):
        began = datetime.now(UTC)
        root = recordings_directory(home)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory = root / (began.strftime("%Y-%m-%d_%H-%M-%S") + "_" + uuid4().hex[:12])
        self.directory.mkdir(mode=0o700)
        self.audio = None
        self.audio_file = None
        self.events = None
        self.samples = 0
        self.decodes = 0
        self.error = ""
        self.closed = False
        self.info = {
            "version": 1,
            "session_id": session_id,
            "started_at": began.isoformat(),
            "status": "recording",
            "model": settings.model,
            "sample_rate": RATE,
            "channels": 1,
            "format": "pcm16",
            "settings": {"recognition_pause": settings.recognition_pause},
        }
        try:
            audio_path = self.directory / "audio.wav"
            self.audio_file = audio_path.open("xb")
            audio_path.chmod(0o600)
            self.audio = wave.open(self.audio_file, "wb")
            self.audio.setnchannels(1)
            self.audio.setsampwidth(2)
            self.audio.setframerate(RATE)
            events_path = self.directory / "events.jsonl"
            self.events = events_path.open("x", encoding="utf-8")
            events_path.chmod(0o600)
            self._write_info()
        except OSError:
            self._close_files()
            raise

    def _write_info(self):
        temporary = self.directory / "session.tmp"
        temporary.write_text(json.dumps(self.info, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.directory / "session.json")

    def append(self, data: bytes):
        if self.closed:
            return
        try:
            self.audio.writeframesraw(data)
            self.samples += len(data) // 2
        except OSError as error:
            self._fail(error)

    def event(self, event: dict):
        if self.closed:
            return
        try:
            self.events.write(json.dumps(event, ensure_ascii=False) + "\n")
            self.events.flush()
        except OSError as error:
            self._fail(error)

    def decode_started(self, offset: float, samples: int) -> int:
        self.decodes += 1
        start = round(offset * RATE)
        self.event(
            {
                "type": "decode_start",
                "index": self.decodes,
                "start_sample": start,
                "end_sample": start + samples,
            }
        )
        return self.decodes

    def finish(self, status: str, text: str):
        if self.closed:
            return
        try:
            self.audio.close()
            self.audio_file.close()
            self.events.close()
            self.info.update(
                status=status,
                finished_at=datetime.now(UTC).isoformat(),
                audio_samples=self.samples,
                audio_seconds=self.samples / RATE,
                decode_count=self.decodes,
                text=text,
            )
            self._write_info()
            self.closed = True
        except OSError as error:
            self._fail(error)

    def _fail(self, error: OSError):
        # EXC-0003: close failed archive, preserve ASR; see docs/exceptional_execution_paths.md.
        self.error = str(error)
        log.warning("Не удалось сохранить аудио: %s", self.error)
        self.closed = True
        self._close_files()

    def _close_files(self):
        for handle in (self.audio, self.audio_file, self.events):
            if handle is not None:
                try:
                    handle.close()
                except OSError as error:
                    log.warning("Не удалось закрыть файл аудиоархива: %s", error)
