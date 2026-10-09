"""Send session events and archive accepted audio without blocking dictation on disk errors."""

import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from .config import Settings
from .recording import SessionRecording

log = logging.getLogger(__name__)


class SessionOutput:
    def __init__(
        self,
        settings: Settings,
        session_id: str,
        send: Callable[[str], Awaitable[None]],
        home: Path | None = None,
    ):
        self.settings = settings
        self.session_id = session_id
        self.send_message = send
        self.recording = None
        self.recording_error = ""
        self.recording_error_sent = False
        if settings.save_recordings:
            # EXC-0003: approved archive degradation; see docs/exceptional_execution_paths.md.
            try:
                self.recording = SessionRecording(settings, session_id, home)
            except OSError as error:
                self.recording_error = str(error)

    async def ready(self, model: str):
        event = {
            "type": "ready",
            "model": model,
            "recognize_on_pause": True,
            "recognition_pause": self.settings.recognition_pause,
        }
        if self.settings.save_recordings:
            if self.recording is not None:
                event["recording_path"] = str(self.recording.directory)
            else:
                event["recording_error"] = self.recording_error
                log.warning("Не удалось создать запись аудио: %s", self.recording_error)
        await self.send(event)

    async def _report_recording_error(self):
        if self.recording is None or not self.recording.error or self.recording_error_sent:
            return
        self.recording_error_sent = True
        await self.send_message(
            json.dumps(
                {
                    "type": "recording",
                    "status": "error",
                    "message": self.recording.error,
                    "session_id": self.session_id,
                },
                ensure_ascii=False,
            )
        )

    async def send(self, event: dict):
        event = {**event, "session_id": self.session_id}
        if self.recording is not None:
            self.recording.event(event)
            if event["type"] in ("session_end", "cancelled"):
                status = "completed" if event["type"] == "session_end" else "cancelled"
                self.close(status, event["text"])
                if not self.recording.error:
                    event["recording_path"] = str(self.recording.directory)
        # ready must remain the first reply, including when creating the journal failed.
        if event["type"] != "ready":
            await self._report_recording_error()
        await self.send_message(json.dumps(event, ensure_ascii=False))
        if event["type"] == "ready":
            await self._report_recording_error()

    async def error(self, message: str, text: str):
        event = {"type": "error", "message": message}
        if self.recording is not None:
            self.recording.event(event)
            self.close("error", text)
            if not self.recording.error:
                event["recording_path"] = str(self.recording.directory)
        await self._report_recording_error()
        await self.send_message(json.dumps(event, ensure_ascii=False))

    async def append(self, packet: bytes):
        if self.recording is not None:
            self.recording.append(packet)
            await self._report_recording_error()

    def decode_started(self, offset: float, samples: int):
        return self.recording.decode_started(offset, samples) if self.recording else None

    def decode_finished(self, event: dict):
        if self.recording is not None:
            self.recording.event(event)

    def close(self, outcome: str, text: str):
        if self.recording is not None:
            self.recording.finish(outcome, text)
