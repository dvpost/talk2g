from __future__ import annotations

import re

from .model import Word


def key(text: str) -> str:
    return re.sub(r"[^\w]", "", text.lower().replace("ё", "е"))


def join_words(words: list[Word]) -> str:
    text = " ".join(word.text.strip() for word in words if word.text.strip())
    return re.sub(r"\s+([,.;:!?%)\]])", r"\1", text).strip()


class Transcript:
    """LocalAgreement-2 plus temporal anchors. Confirmed input is append-only."""

    def __init__(self, holdback: float = 0.8):
        self.holdback = holdback
        self.frontier = -1.0
        self.anchors: list[Word] = []
        self.previous: list[Word] = []
        self.text = ""
        self.sequence = 0

    def remaining(self, words: list[Word]) -> list[Word]:
        if not self.anchors:
            return words
        # Search only near actual time; repeated phrases at other times must survive.
        for size in range(min(4, len(self.anchors)), 0, -1):
            anchor = self.anchors[-size:]
            candidates = []
            for i in range(len(words) - size + 1):
                candidate = words[i : i + size]
                if [key(w.text) for w in candidate] != [key(w.text) for w in anchor]:
                    continue
                distance = abs(candidate[-1].end - anchor[-1].end)
                if distance <= 0.45:
                    candidates.append((distance, i + size))
            if candidates:
                _, index = min(candidates)
                return words[index:]
        return [
            word for word in words if word.start >= self.frontier - 0.02 and word.end > self.frontier + 0.02
        ]

    def update(
        self, words: list[Word], audio_end: float, *, final: bool = False, forced: bool = False
    ) -> tuple[str, str]:
        remaining = self.remaining(words)
        if final and not forced:
            count = len(remaining)
        else:
            count = 0
            for current, previous in zip(remaining, self.previous, strict=False):
                if key(current.text) != key(previous.text) or abs(current.start - previous.start) > 0.5:
                    break
                if current.end > audio_end - self.holdback:
                    break
                count += 1
            # Never commit the final word of a running window, even if its tokens agree.
            count = min(count, max(0, len(remaining) - 1))
            if forced:
                count = sum(word.end <= audio_end - max(self.holdback, 0.7) for word in remaining[:-1])
        committed = remaining[:count]
        delta = ""
        if committed:
            phrase = join_words(committed)
            prefix = " " if self.text and phrase and phrase[0] not in ",.;:!?%)]" else ""
            delta = prefix + phrase
            self.text += delta
            self.frontier = committed[-1].end
            self.anchors = (self.anchors + committed)[-6:]
            self.sequence += 1
        self.previous = remaining[count:]
        if final:
            self.previous = []
        return delta, join_words(remaining[count:])
