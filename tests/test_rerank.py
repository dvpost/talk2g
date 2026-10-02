import asyncio
import json
import threading

import numpy as np
import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from giga_dictation.audio import pcm
from giga_dictation.config import RATE, Settings
from giga_dictation.dual_window import WindowAgreement
from giga_dictation.language_model import Ranking
from giga_dictation.model import Word
from giga_dictation.rerank import WindowReranker
from giga_dictation.server import DictationServer


class Judge:
    def __init__(self, choice=1):
        self.choice = choice
        self.calls = []

    def rank(self, first, second, **kwargs):
        self.calls.append((first, second, kwargs))
        return Ranking(self.choice, (-7.0, -5.0), 0.01, "selected" if self.choice is not None else "tie")


@pytest.mark.parametrize("choice,expected", [(0, "кот"), (1, "код"), (None, "кот")])
def test_judge_can_choose_either_window_or_abstain(choice, expected):
    agreement = WindowAgreement(0.2)
    long = [Word("этот", 0, 0.2), Word("кот", 0.4, 0.6), Word("работает", 1, 1.2)]
    short = [Word("этот", 0, 0.2), Word("код", 0.4, 0.6), Word("работает", 1, 1.2)]
    agreement.preview(short, 0, 2)
    model = Judge(choice)
    selection = WindowReranker(model, 0.12).select(agreement, long, 2)
    assert selection.words[1].text == expected
    assert len(model.calls) == 1
    assert model.calls[0][:2] == ("этот кот работает", "этот код работает")
    assert len(selection.confirmed) == (1 if choice is None else 3)


@pytest.mark.parametrize(
    "first,second", [("25", "38"), ("пять", "шесть"), ("Даниил", "Данил"), ("foo_bar", "foo_baz")]
)
def test_sensitive_substitutions_do_not_reach_language_model(first, second):
    agreement = WindowAgreement(0.2)
    long = [Word(first, 0.5, 0.7)]
    agreement.preview([Word(second, 0.5, 0.7)], 0, 2)
    model = Judge()
    selection = WindowReranker(model, 0.12).select(agreement, long, 2)
    assert selection.words == long
    assert not model.calls and selection.details["protected"] == 1


def test_same_words_do_not_run_judge_and_neighbouring_word_cannot_be_substituted():
    agreement = WindowAgreement(0.2)
    words = [Word("код", 0.1, 0.2), Word("работает", 0.6, 0.8)]
    agreement.preview(words, 0, 2)
    model = Judge()
    ranker = WindowReranker(model, 0.12)
    assert ranker.select(agreement, words, 2).words == words
    distant = [Word("кот", 0.39, 0.49), words[1]]
    assert ranker.select(agreement, distant, 2).words == distant
    assert not model.calls


def test_short_choice_survives_next_long_pass_and_preserves_overlapping_next_word():
    agreement = WindowAgreement(0.2)
    short = [Word("код", 0, 0.3), Word("работает", 0.25, 0.6)]
    long = [Word("кот", 0, 0.3), short[1]]
    agreement.preview(short, 0, 1)
    ranker = WindowReranker(Judge(), 0.12)
    selected = ranker.select(agreement, long, 1)
    agreement.transcript.previous = selected.confirmed
    assert agreement.transcript.update(selected.words, 1)[0] == "код"
    # The next word starts before the previous token's end. Time-only filtering
    # of an already-trimmed list would lose it; the full anchored list must survive.
    extended = long + [Word("хорошо", 0.9, 1.1)]
    agreement.preview(short + [extended[-1]], 0, 2)
    selected = ranker.select(agreement, extended, 2)
    agreement.transcript.previous = selected.confirmed
    assert agreement.transcript.update(selected.words, 2, final=True)[0] == " работает хорошо"
    assert agreement.transcript.text == "код работает хорошо"


def test_judge_can_recover_an_older_short_hypothesis_when_latest_windows_agree_on_error():
    agreement = WindowAgreement(0.2)
    good = [Word("код", 0.1, 0.3), Word("работает", 0.6, 0.8)]
    bad = [Word("кот", 0.1, 0.3), good[1]]
    agreement.preview(good, 0, 1)
    agreement.preview(bad, 0, 1.5)
    model = Judge()
    selection = WindowReranker(model, 0.12).select(agreement, bad, 1.5)
    assert selection.words == good
    assert model.calls and selection.details["choice"] == "short"


def test_committed_alias_does_not_rewrite_a_following_similar_word():
    agreement = WindowAgreement(0.2)
    original = Word("кот", 0, 0.15)
    chosen = Word("код", 0, 0.15)
    agreement.transcript.update([chosen], 1, final=True)
    ranker = WindowReranker(Judge(), 0.12)
    ranker.aliases.append((original, chosen))
    following = Word("кот", 0.1, 0.2)
    assert ranker.project_committed([original, following], agreement.transcript) == [chosen, following]


class Detector:
    def probability(self, frame):
        return float(np.max(np.abs(frame)) > 0.01)


class SignalRecognizer:
    def decode(self, audio):
        offset = round((float(audio[0]) - 0.1) / 0.01, 1)
        end = offset + len(audio) / RATE
        words = []
        for index in range(10):
            if offset <= index + 0.2 and index + 0.4 <= end:
                text = "код" if index == 1 else f"слово{index}"
                if index == 1 and len(audio) / RATE > 2.05:
                    text = "кот"
                words.append(Word(text, index + 0.2 - offset, index + 0.4 - offset))
        return words


async def send_start(socket, **overrides):
    await socket.send(
        json.dumps(
            {
                "type": "start",
                "version": 1,
                "rate": RATE,
                "format": "pcm16",
                "dual_window": True,
                "lm_rescore": True,
                "interval": 0.25,
                "quality_interval": 0.5,
                "fast_window": 2,
                "quality_window": 4,
                "holdback": 0.2,
                "quality_holdback": 0.2,
                **overrides,
            }
        )
    )
    return json.loads(await socket.recv())


async def collect(socket, events=None):
    events = [] if events is None else events
    async for message in socket:
        event = json.loads(message)
        events.append(event)
        if event["type"] in ("session_end", "error", "cancelled"):
            return events


async def test_neural_mode_real_websocket_substitution_before_stop_and_overlap_integrity():
    model = Judge()
    service = DictationServer(
        Settings(), recognizer=SignalRecognizer(), detector_factory=Detector, language_model=model
    )
    audio = 0.1 + np.arange(10 * RATE, dtype=np.float32) / RATE * 0.01
    try:
        async with serve(service.handle, "127.0.0.1", 0) as listener:
            port = listener.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}/v1/dictate", proxy=None) as socket:
                assert (await send_start(socket))["lm_rescore"] is True
                live = []
                receiver = asyncio.create_task(collect(socket, live))
                for offset in range(0, len(audio), 1600):
                    await socket.send(pcm(audio[offset : offset + 1600]))
                    await asyncio.sleep(0.025)
                assert "код" in "".join(e["delta"] for e in live if e["type"] == "commit")
                await socket.send('{"type":"stop"}')
                events = await asyncio.wait_for(receiver, 4)
        assert events[-1]["type"] == "session_end", events
        expected = " ".join("код" if i == 1 else f"слово{i}" for i in range(10))
        assert events[-1]["text"] == expected
        commits = [e for e in events if e["type"] == "commit"]
        assert "".join(e["delta"] for e in commits) == expected
        assert [e["seq"] for e in commits] == list(range(1, len(commits) + 1))
        assert any(e["type"] == "rescore" and e["choice"] == "short" for e in events)
        assert events[-1]["dual_window"]["language_model"]["short_choices"] > 0
        assert model.calls
    finally:
        service.pool.shutdown(wait=True, cancel_futures=True)


async def test_missing_language_model_reports_error_and_releases_server(tmp_path):
    service = DictationServer(
        Settings(), recognizer=SignalRecognizer(), detector_factory=Detector, home=tmp_path
    )
    try:
        async with serve(service.handle, "127.0.0.1", 0) as listener:
            port = listener.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}/v1/dictate", proxy=None) as socket:
                error = await send_start(socket)
                assert error["type"] == "error" and "download --language-model" in error["message"]
            async with connect(f"ws://127.0.0.1:{port}/v1/dictate", proxy=None) as socket:
                ready = await send_start(socket, dual_window=False)
                assert ready["type"] == "ready" and ready["lm_rescore"] is False
                await socket.send('{"type":"stop"}')
                assert (await collect(socket))[-1]["type"] == "session_end"
    finally:
        service.pool.shutdown(wait=True, cancel_futures=True)


async def test_cancel_during_language_model_inference_does_not_wait_for_native_call():
    entered, release = threading.Event(), threading.Event()

    class BlockingJudge(Judge):
        def rank(self, *args, **kwargs):
            entered.set()
            assert release.wait(5)
            return super().rank(*args, **kwargs)

    service = DictationServer(
        Settings(), recognizer=SignalRecognizer(), detector_factory=Detector, language_model=BlockingJudge()
    )
    audio = 0.1 + np.arange(3 * RATE, dtype=np.float32) / RATE * 0.01
    try:
        async with serve(service.handle, "127.0.0.1", 0) as listener:
            port = listener.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}/v1/dictate", proxy=None) as socket:
                assert (await send_start(socket))["type"] == "ready"
                receiver = asyncio.create_task(collect(socket))
                for offset in range(0, len(audio), 1600):
                    await socket.send(pcm(audio[offset : offset + 1600]))
                    await asyncio.sleep(0.025)
                assert await asyncio.to_thread(entered.wait, 2)
                await socket.send('{"type":"cancel"}')
                assert (await asyncio.wait_for(receiver, 0.5))[-1]["type"] == "cancelled"
    finally:
        release.set()
        service.pool.shutdown(wait=True, cancel_futures=True)
