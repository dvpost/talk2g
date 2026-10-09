from __future__ import annotations

import re

from .model import Word


def join_words(words: list[Word]) -> str:
    text = " ".join(word.text.strip() for word in words if word.text.strip())
    return re.sub(r"\s+([,.;:!?%)\]])", r"\1", text).strip()


class Transcript:
    """Append independently recognized whole blocks, preserving spoken repetitions."""

    def __init__(self):
        self.frontier = -1.0
        self.text = ""
        self.sequence = 0

    def commit_block(self, words: list[Word]) -> str:
        """Append one independently recognized block, preserving spoken repetitions."""
        phrase = join_words(words)
        if not phrase:
            return ""
        prefix = " " if self.text and phrase[0] not in ",.;:!?%)]" else ""
        delta = prefix + phrase
        self.text += delta
        self.frontier = words[-1].end
        self.sequence += 1
        return delta
