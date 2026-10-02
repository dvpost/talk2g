import numpy as np
import pytest
import soundfile as sf

from giga_dictation.audio import Segmenter, pcm, read_audio
from giga_dictation.config import RATE, Settings


class Detector:
    def probability(self, frame):
        return float(np.max(np.abs(frame)) > 0.05)


def test_capture_keeps_preroll_first_word_and_subframe_stop_tail():
    settings = Settings(window=4)
    segmenter = Segmenter(Detector(), settings)
    audio = np.concatenate([np.zeros(1600), np.full(2207, 0.2)]).astype(np.float32)
    packet = pcm(audio)
    # TCP/WebSocket packet boundaries must not become recognition boundaries.
    for offset in range(0, len(packet), 114):
        segmenter.feed(packet[offset : offset + 114])
    segmenter.stop()
    assert len(segmenter.finished) == 1
    captured, offset = segmenter.finished[0].snapshot()
    assert offset == 0
    np.testing.assert_allclose(captured, audio, atol=1 / 16000)


def test_silence_ends_phrase_without_waiting_for_recording_stop():
    segmenter = Segmenter(Detector(), Settings(silence=0.3))
    segmenter.feed(pcm(np.full(RATE, 0.2)))
    segmenter.feed(pcm(np.zeros(RATE)))
    assert segmenter.current is None
    assert len(segmenter.finished) == 1
    assert segmenter.finished[0].closed


def test_long_speech_has_bounded_windows_and_overlap():
    segmenter = Segmenter(Detector(), Settings(window=4))
    segmenter.feed(pcm(np.full(RATE * 12, 0.2)))
    segmenter.stop()
    segments = list(segmenter.finished)
    assert len(segments) >= 3
    assert all(s.count <= 4 * RATE + 512 for s in segments)
    assert all(a.end - b.start == int(1.5 * RATE) for a, b in zip(segments, segments[1:], strict=False))
    assert segments[-1].end == RATE * 12


def test_empty_and_incomplete_pcm_is_refused():
    segmenter = Segmenter(Detector(), Settings())
    for packet in (b"", b"x"):
        with pytest.raises(ValueError, match="PCM16"):
            segmenter.feed(packet)


def test_large_holdback_keeps_all_unconfirmed_audio_at_forced_cut():
    settings = Settings(window=4, holdback=3)
    segmenter = Segmenter(Detector(), settings)
    segmenter.feed(pcm(np.full(RATE * 5, 0.2)))
    segments = list(segmenter.finished) + [segmenter.current]
    assert len(segments) >= 2
    for previous, following in zip(segments, segments[1:], strict=False):
        # Every sample that was too fresh to commit must still be present in the
        # following decode, with extra context for words crossing the boundary.
        unconfirmed_start = previous.end - settings.holdback * RATE
        assert following.start < unconfirmed_start
        assert previous.end <= following.end


def test_stereo_file_is_resampled_with_right_length(tmp_path):
    path = tmp_path / "stereo.wav"
    t = np.arange(4800) / 48000
    signal = 0.5 * np.sin(2 * np.pi * 1000 * t)
    sf.write(path, np.column_stack([signal, signal]), 48000)
    result = read_audio(path)
    assert result.shape == (1600,)
    assert np.max(np.abs(result)) > 0.4
