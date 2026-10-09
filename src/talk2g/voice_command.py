from __future__ import annotations

import re


class VoiceStop:
    """Filter confirmed text before insertion, retaining a possible split command."""

    command = re.compile(r"(?<!\w)конец[^\w]+связи(?!\w)", re.IGNORECASE)

    def __init__(self):
        self.pending = ""
        self.triggered = False

    def feed(self, delta: str, *, enabled: bool, final: bool = False) -> tuple[str, bool]:
        if self.triggered:
            return "", False  # queued audio after the command must not become dictated text
        text = self.pending + delta
        self.pending = ""
        if not enabled:
            return text, False
        if match := self.command.search(text):
            self.triggered = True
            return text[: match.start()].rstrip(), True
        if final:
            return text.rstrip(), False
        words = list(re.finditer(r"\w+", text))
        start = len(text)
        if words:
            last = words[-1]
            if "конец".startswith(last[0].casefold()):
                start = last.start()
            if len(words) > 1 and words[-2][0].casefold() == "конец":
                if "связи".startswith(last[0].casefold()):
                    start = words[-2].start()
        safe = text[:start].rstrip()
        self.pending = text[len(safe) :]
        return safe, False
