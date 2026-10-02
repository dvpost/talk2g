"""Rescore time-aligned ASR substitutions; preserve unsupported/sensitive speech."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .model import Word
from .transcript import join_words, key

NUMBER_WORDS = set(
    "ноль нуль один одна одно два две три четыре пять шесть семь восемь девять десять "
    "одиннадцать двенадцать тринадцать четырнадцать пятнадцать шестнадцать семнадцать "
    "восемнадцать девятнадцать двадцать тридцать сорок пятьдесят шестьдесят семьдесят "
    "восемьдесят девяносто сто двести триста четыреста пятьсот шестьсот семьсот восемьсот "
    "девятьсот тысяча тысячи тысяч миллион миллиона миллионов миллиард миллиарда миллиардов".split()
)


def sensitive(text: str) -> bool:
    return (
        any(character.isdigit() or character in "_/@\\=" for character in text)
        or bool(text and text[0].isupper())
        or key(text) in NUMBER_WORDS
    )


@dataclass
class Selection:
    words: list[Word]
    confirmed: list[Word]
    details: dict


class WindowReranker:
    def __init__(self, model, margin: float):
        self.model = model
        self.margin = margin
        self.aliases = deque(maxlen=32)

    def project_committed(self, words, transcript):
        result = []
        for word in words:
            anchor = next(
                (
                    committed
                    for original, selected in reversed(self.aliases)
                    for committed in transcript.anchors
                    if key(word.text) == key(original.text)
                    and key(committed.text) == key(selected.text)
                    and abs(word.start - original.start) <= 0.2
                    and abs(word.end - committed.end) <= 0.2
                    and word.end <= transcript.frontier + 0.04
                ),
                None,
            )
            result.append(Word(anchor.text, word.start, word.end) if anchor else word)
        return result

    def select(self, agreement, words, end) -> Selection:
        words = self.project_committed(words, agreement.transcript)
        remaining = agreement.transcript.remaining(words)
        aligned = agreement.aligned(remaining)
        candidates = {}
        protected = set()
        for snapshot in reversed(agreement.fast):
            alternate = list(remaining)
            changed = []
            historical = agreement.aligned(remaining, [snapshot])
            for index, (word, other) in enumerate(zip(remaining, historical, strict=True)):
                if other is None or key(word.text) == key(other.text):
                    continue
                if sensitive(word.text) or sensitive(other.text):
                    protected.add(index)
                    continue
                # Replacement requires closer time alignment than agreement.
                if abs(word.start - other.start) > 0.2 or abs(word.end - other.end) > 0.25:
                    continue
                alternate[index] = Word(other.text, word.start, word.end)
                changed.append(index)
            if changed:
                text = join_words(alternate)
                if text in candidates:
                    candidates[text][2] += 1
                else:
                    candidates[text] = [alternate, changed, 1]
        details = {"compared": False, "choice": "unchanged", "protected": len(protected), "seconds": 0.0}
        selected = remaining
        resolved = set()
        judged = False
        evaluated = 0
        # Bound CPU work even if many small-window hypotheses disagree.
        for alternate, changed, _ in sorted(candidates.values(), key=lambda item: -item[2])[:3]:
            ranking = self.model.rank(
                join_words(selected),
                join_words(alternate),
                context=agreement.transcript.text[-1200:],
                margin=self.margin,
            )
            details.update(
                compared=True,
                reason=ranking.reason,
                scores=ranking.scores,
            )
            details["seconds"] += ranking.seconds
            evaluated += 1
            if ranking.choice is not None:
                judged = True
                selected = selected if ranking.choice == 0 else alternate
                resolved.update(changed)
        if candidates:
            changes = [
                i
                for i, (a, b) in enumerate(zip(remaining, selected, strict=True))
                if key(a.text) != key(b.text)
            ]
            details.update(
                choice=("short" if changes else "long") if judged else "abstain",
                substitutions=len(changes),
                evaluated=evaluated,
            )
            self.aliases.extend((remaining[index], selected[index]) for index in changes)
        confirmed = []
        for index, (word, other) in enumerate(zip(selected, aligned, strict=True)):
            if index in resolved or (other is not None and key(word.text) == key(other.text)):
                confirmed.append(word)
            else:
                break
        # A short-window choice inherits the long word's time. This keeps already
        # inserted anchors stable at the next overlap even if the spelling differs.
        replacements = {id(original): chosen for original, chosen in zip(remaining, selected, strict=True)}
        return Selection([replacements.get(id(word), word) for word in words], confirmed, details)
