from types import SimpleNamespace

import numpy as np
import pytest

from talk2g import cli
from talk2g.audio import pcm
from talk2g.config import RATE, Settings
from talk2g.model import Word


@pytest.mark.parametrize("pause,blocks", [(2, 2), (5, 1)])
def test_transcribe_uses_configured_pauses_instead_of_fixed_windows(monkeypatch, pause, blocks):
    audio = np.concatenate([np.full(8 * RATE, 0.2), np.zeros(3 * RATE), np.full(RATE + 207, 0.3)])
    inputs = []

    def decode(block):
        inputs.append(block.copy())
        return [Word(f"Блок{len(inputs)}.", 0, 0.2)]

    monkeypatch.setattr(cli, "read_audio", lambda path: audio)
    monkeypatch.setattr(cli, "GigaRecognizer", lambda settings: SimpleNamespace(decode=decode, vad_path=None))
    monkeypatch.setattr(
        cli,
        "SileroDetector",
        lambda path: SimpleNamespace(probability=lambda frame: float(np.max(np.abs(frame)) > 0.05)),
    )
    text = cli.transcribe("test.wav", Settings(window=4, recognition_pause=pause))
    assert len(inputs) == blocks
    assert text == " ".join(f"Блок{number}." for number in range(1, blocks + 1))
    expected_tail = np.frombuffer(pcm(audio[-207:]), dtype="<i2").astype(np.float32) / 32768
    np.testing.assert_array_equal(inputs[-1][-207:], expected_tail)
