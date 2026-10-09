import numpy as np
import pytest
import soundfile as sf

from talk2g.audio import Segmenter, SpeechTimeout, pcm, read_audio
from talk2g.config import RATE, Settings


class Detector:
    def probability(self, frame):
        return float(np.max(np.abs(frame)) > 0.05)


def test_idle_timeout_counts_from_start_and_ignores_silence_and_clicks():
    timeout = SpeechTimeout(Detector(), 45, started_at=100)
    timeout.feed(pcm(np.zeros(RATE)), captured_at=101)
    timeout.feed(pcm(np.full(512, 0.2)), captured_at=102)
    timeout.feed(pcm(np.zeros(RATE)), captured_at=103)
    assert timeout.remaining(103) == 42
    assert timeout.remaining(145) == timeout.remaining(150) == 0


def test_speech_resets_idle_timeout_even_during_continuous_speech():
    timeout = SpeechTimeout(Detector(), 45, started_at=100)
    speech = pcm(np.full(512 * 3, 0.2))
    timeout.feed(speech, captured_at=140)
    assert timeout.remaining(140) == 45
    timeout.feed(speech, captured_at=180)
    assert timeout.remaining(180) == 45
    timeout.feed(pcm(np.zeros(512 * 3)), captured_at=181)
    assert timeout.remaining(200) == 25
    timeout.feed(speech, captured_at=210)
    assert timeout.remaining(210) == 45
    assert timeout.remaining(255) == 0


def test_idle_timeout_uses_capture_time_for_delayed_audio_and_keeps_partial_frames():
    timeout = SpeechTimeout(Detector(), 45, started_at=100)
    # A packet ends with silence; the reset belongs to the last voiced frame.
    audio = np.concatenate([np.full(512 * 3, 0.2), np.zeros(512 * 4)])
    packet = pcm(audio)
    timeout.feed(packet[:200], captured_at=110 - (len(audio) - 100) / RATE)
    timeout.feed(packet[200:], captured_at=110)
    assert timeout.remaining(150) == pytest.approx(5 - 512 * 4 / RATE)
    assert timeout.remaining(155) == 0


def test_quieter_speech_after_short_pause_resets_countdown_with_server_vad_hysteresis():
    class Probability:
        def probability(self, frame):
            return float(frame.max())

    timeout = SpeechTimeout(Probability(), 45, started_at=0, release_after=3)
    timeout.feed(pcm(np.full(512 * 3, 0.9)), captured_at=1)
    timeout.feed(pcm(np.zeros(512 * 30)), captured_at=2)
    timeout.feed(pcm(np.full(512, 0.4)), captured_at=2.1)
    assert timeout.last_speech == 2.1 and timeout.remaining(2.1) == 45
    timeout.feed(pcm(np.zeros(512 * 100)), captured_at=5.3)
    timeout.feed(pcm(np.full(512 * 3, 0.4)), captured_at=5.4)
    assert timeout.last_speech == 2.1  # after the full pause, starting speech requires p >= 0.5


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
    segmenter = Segmenter(Detector(), Settings(silence=0.3, recognize_on_pause=False))
    segmenter.feed(pcm(np.full(RATE, 0.2)))
    segmenter.feed(pcm(np.zeros(RATE)))
    assert segmenter.current is None
    assert len(segmenter.finished) == 1
    assert segmenter.finished[0].closed


def test_long_speech_has_bounded_windows_and_overlap():
    segmenter = Segmenter(Detector(), Settings(window=4, recognize_on_pause=False))
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
    settings = Settings(window=4, holdback=3, recognize_on_pause=False)
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


def test_pause_mode_accumulates_long_speech_without_window_cuts():
    segmenter = Segmenter(Detector(), Settings(window=4, recognition_pause=3))
    segmenter.feed(pcm(np.full(65 * RATE, 0.2)))
    assert not segmenter.finished
    assert segmenter.current.count > 64 * RATE
    segmenter.stop()
    assert len(segmenter.finished) == 1 and not segmenter.finished[0].forced
    assert segmenter.finished[0].end == 65 * RATE


def test_short_pause_resets_and_long_pause_submits_one_complete_block():
    segmenter = Segmenter(Detector(), Settings(recognition_pause=2))
    speech = np.full(512 * 40, 0.2)
    silence = np.zeros(512 * 40)  # 1.28 seconds: below the recognition deadline
    segmenter.feed(pcm(speech))
    segmenter.feed(pcm(silence))
    segmenter.feed(pcm(speech))
    segmenter.feed(pcm(silence))
    assert not segmenter.finished
    segmenter.feed(pcm(np.zeros(512 * 24)))
    assert segmenter.current is None and len(segmenter.finished) == 1
    captured, _ = segmenter.finished[0].snapshot()
    expected = np.concatenate([speech, silence, speech, np.zeros(int(0.3 * RATE))])
    np.testing.assert_allclose(captured, expected, atol=1 / 16000)


def test_pause_mode_stop_flushes_unexpired_silence_and_every_subframe_sample():
    segmenter = Segmenter(Detector(), Settings(recognition_pause=5))
    audio = np.concatenate([np.full(RATE, 0.2), np.zeros(3207)])
    segmenter.feed(pcm(audio))
    assert not segmenter.finished
    segmenter.stop()
    captured, _ = segmenter.finished[0].snapshot()
    np.testing.assert_allclose(captured, audio, atol=1 / 16000)
