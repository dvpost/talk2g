from concurrent.futures import Executor, Future

import numpy as np
import pytest

from talk2g.config import Settings
from talk2g.model import Word
from talk2g.session import RecognitionSession


class ImmediateExecutor(Executor):
    def submit(self, fn, /, *args, **kwargs):
        future = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except Exception as error:
            future.set_exception(error)
        return future


class Detector:
    def probability(self, frame):
        return float(np.max(np.abs(frame)) > 0.01)


class Recognizer:
    def __init__(self):
        self.audio = []

    def decode(self, audio):
        self.audio.append(audio.copy())
        return [Word("Целый блок.", 0, 0.05)]


class Output:
    def __init__(self):
        self.events = []
        self.decodes = []

    async def send(self, event):
        self.events.append(event)

    def decode_started(self, offset, samples):
        self.decodes.append((offset, samples))
        return 1

    def decode_finished(self, event):
        self.events.append(event)


async def test_stop_decodes_the_entire_block_including_incomplete_vad_frame():
    # GIVEN: речь из трёх кадров VAD и неполного хвоста, часы зафиксированы.
    recognizer, output = Recognizer(), Output()
    session = RecognitionSession(Settings(), Detector(), recognizer, ImmediateExecutor(), clock=lambda: 100)
    packet = np.full(1743, 8192, dtype="<i2").tobytes()
    session.feed(packet)
    session.stop()
    # WHEN: сессия обрабатывает готовые блоки после Stop.
    await session.run(output)
    # THEN: модель получает все сэмплы один раз, итог идёт после commit и завершения блока.
    assert len(recognizer.audio) == 1
    np.testing.assert_array_equal(recognizer.audio[0], np.full(1743, 0.25, dtype=np.float32))
    assert output.decodes == [(0, 1743)]
    assert output.events == [
        {"type": "recognizing", "audio_start": 0, "audio_end": 0.1089375, "capturing": False},
        {
            "type": "decode_end",
            "index": 1,
            "words": [{"text": "Целый блок.", "start": 0, "end": 0.05}],
            "delta": "Целый блок.",
            "final": True,
            "decode_seconds": 0,
        },
        {"type": "commit", "seq": 1, "delta": "Целый блок.", "audio_end": 0.05, "decode_seconds": 0},
        {"type": "segment_end"},
        {"type": "session_end", "text": "Целый блок.", "audio_seconds": 0.1089375, "elapsed_seconds": 0},
    ]
    assert session.text == "Целый блок."


async def test_stop_without_speech_finishes_without_calling_recognition():
    # GIVEN: сессия получила один кадр тишины.
    recognizer, output = Recognizer(), Output()
    session = RecognitionSession(Settings(), Detector(), recognizer, ImmediateExecutor(), clock=lambda: 100)
    session.feed(bytes(1024))
    session.stop()
    # WHEN: завершается обработка аудио.
    await session.run(output)
    # THEN: модель не вызывается, итог содержит длительность принятой тишины и пустой текст.
    assert recognizer.audio == []
    assert output.events == [
        {"type": "session_end", "text": "", "audio_seconds": 0.032, "elapsed_seconds": 0},
    ]


@pytest.mark.parametrize(
    ("packet", "message"),
    [
        pytest.param(b"", "целые PCM16", id="empty"),
        pytest.param(b"\x01", "целые PCM16", id="partial-sample"),
        pytest.param(bytes(32002), "одну секунду", id="packet-too-long"),
    ],
)
def test_invalid_pcm_packet_is_rejected_before_recognition(packet, message):
    # GIVEN: новая сессия с тестовым детектором речи.
    recognizer = Recognizer()
    session = RecognitionSession(Settings(), Detector(), recognizer, ImmediateExecutor(), clock=lambda: 100)
    # WHEN: поступает неверный PCM-пакет.
    with pytest.raises(ValueError, match=message):
        session.feed(packet)
    # THEN: распознавание не запускается, подтверждённого текста нет.
    assert recognizer.audio == [] and session.text == ""
