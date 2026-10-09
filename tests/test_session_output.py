import json
import wave
from pathlib import Path

import pytest

from talk2g import session_output
from talk2g.config import Settings
from talk2g.session_output import SessionOutput


@pytest.mark.parametrize(
    ("terminal", "status"),
    [
        pytest.param("session_end", "completed", id="completed"),
        pytest.param("cancelled", "cancelled", id="cancelled"),
        pytest.param("error", "error", id="failed"),
    ],
)
async def test_archive_is_finalized_before_terminal_event_reaches_client(tmp_path, terminal, status):
    # GIVEN: архив с шестью точными PCM-сэмплами и двумя входными диапазонами модели.
    packet = b"\x01\x00\xff\xff\x00\x20\x00\x10\x00\x00\x00\x80"
    events, checkpoint = [], []

    async def send(message):
        event = json.loads(message)
        events.append(event)
        if event["type"] == terminal:
            directory = Path(event["recording_path"])
            info = json.loads((directory / "session.json").read_text())
            with wave.open(str(directory / "audio.wav"), "rb") as audio:
                checkpoint.append((info["status"], info["text"], info["audio_samples"], audio.readframes(6)))

    output = SessionOutput(Settings(save_recordings=True), "session", send, tmp_path)
    await output.ready("gigaam-v3-e2e-ctc")
    await output.append(packet)
    first = output.decode_started(0, 3)
    second = output.decode_started(3 / 16000, 3)
    # WHEN: результат отправляется клиенту, затем выполняется очистка разорванного соединения.
    if terminal == "error":
        await output.error("Ошибка модели", "Полученный текст.")
    else:
        await output.send({"type": terminal, "text": "Полученный текст."})
    output.close("disconnected", "")
    # THEN: клиент уже видит закрытый WAV и окончательные метаданные, очистка их не переписывает.
    assert checkpoint == [(status, "Полученный текст.", 6, packet)]
    directory = Path(events[-1]["recording_path"])
    info = json.loads((directory / "session.json").read_text())
    assert (info["status"], info["text"], info["decode_count"], first, second) == (
        status,
        "Полученный текст.",
        2,
        1,
        2,
    )
    journal = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
    assert journal[1:3] == [
        {"type": "decode_start", "index": 1, "start_sample": 0, "end_sample": 3},
        {"type": "decode_start", "index": 2, "start_sample": 3, "end_sample": 6},
    ]


async def test_disabled_archive_sends_unchanged_protocol_without_creating_files(tmp_path):
    # GIVEN: сохранение аудио выключено, распознавание возвращает целые блоки.
    messages = []

    async def send(message):
        messages.append(json.loads(message))

    output = SessionOutput(Settings(), "session", send, tmp_path)
    event = {"type": "commit", "seq": 1, "delta": "Текст."}
    # WHEN: сервер принимает PCM и отправляет начало, фрагмент и итог.
    await output.ready("gigaam-v3-e2e-ctc")
    await output.append(b"\x01\x00")
    await output.send(event)
    await output.send({"type": "session_end", "text": "Текст."})
    output.close("disconnected", "")
    # THEN: архив не создаётся, номера сессии добавляются без изменения исходного события.
    assert list(tmp_path.iterdir()) == []
    assert event == {"type": "commit", "seq": 1, "delta": "Текст."}
    assert messages == [
        {
            "type": "ready",
            "model": "gigaam-v3-e2e-ctc",
            "session_id": "session",
            "recognize_on_pause": True,
            "recognition_pause": 3,
        },
        {"type": "commit", "seq": 1, "delta": "Текст.", "session_id": "session"},
        {"type": "session_end", "text": "Текст.", "session_id": "session"},
    ]


@pytest.mark.parametrize("stage", ["pcm", "journal-write", "journal-flush"])
async def test_archive_io_error_is_reported_once_without_retry_or_losing_text(
    tmp_path, monkeypatch, caplog, stage
):
    # GIVEN: реальный архив с PCM; следующая запись аудио или журнала отклоняется файловой системой.
    events, attempts = [], []

    async def send(message):
        events.append(json.loads(message))

    output = SessionOutput(Settings(save_recordings=True), "session", send, tmp_path)
    await output.ready("gigaam-v3-e2e-ctc")
    await output.append(b"\x01\x00")
    directory = output.recording.directory

    def denied(*args):
        attempts.append(True)
        raise OSError("Диск заполнен")

    if stage == "pcm":
        monkeypatch.setattr(output.recording.audio, "writeframesraw", denied)
    else:
        method = "write" if stage == "journal-write" else "flush"
        monkeypatch.setattr(output.recording.events, method, denied)
    # WHEN: возникает ошибка, после которой приходят новые PCM и результаты распознавания.
    if stage == "pcm":
        await output.append(b"\x02\x00")
    else:
        await output.send({"type": "recognizing", "audio_start": 0, "audio_end": 1})
    # Закрытие TextIOWrapper вызывает flush ещё раз, чтобы освободить ресурс после ошибки.
    attempts_after_cleanup = len(attempts)
    assert attempts_after_cleanup == (2 if stage == "journal-flush" else 1)
    await output.append(b"\x03\x00")
    await output.send({"type": "commit", "seq": 1, "delta": "Текст."})
    await output.send({"type": "commit", "seq": 2, "delta": " Хвост."})
    await output.send({"type": "session_end", "text": "Текст. Хвост."})
    output.close("disconnected", "")
    # THEN: после очистки запись не возобновляется, одно уведомление, полный текст без ложного успеха архива.
    assert len(attempts) == attempts_after_cleanup
    errors = [event for event in events if event["type"] == "recording"]
    assert errors == [
        {"type": "recording", "status": "error", "message": "Диск заполнен", "session_id": "session"}
    ]
    commits = [event for event in events if event["type"] == "commit"]
    assert "".join(event["delta"] for event in commits) == events[-1]["text"] == "Текст. Хвост."
    assert events[-1]["type"] == "session_end" and "recording_path" not in events[-1]
    assert output.recording.closed and output.recording.audio_file.closed and output.recording.events.closed
    assert list((tmp_path / "recordings").iterdir()) == [directory]
    assert "Не удалось сохранить аудио: Диск заполнен" in caplog.text


@pytest.mark.parametrize("terminal", ["session_end", "cancelled", "error", "disconnected"])
@pytest.mark.parametrize("stage", ["wav-header", "metadata"])
async def test_archive_finalization_error_is_visible_without_retry_or_false_success(
    tmp_path, monkeypatch, caplog, terminal, stage
):
    # GIVEN: PCM принят, но финализация заголовка WAV или метаданных отклоняется файловой системой.
    events, attempts = [], []

    async def send(message):
        events.append(json.loads(message))

    output = SessionOutput(Settings(save_recordings=True), "session", send, tmp_path)
    await output.ready("gigaam-v3-e2e-ctc")
    await output.append(b"\x01\x00")
    await output.append(b"\x02\x00")
    await output.send({"type": "commit", "seq": 1, "delta": "Полученный текст."})

    def denied(*args):
        attempts.append(args)
        raise PermissionError("Финализация недоступна")

    if stage == "wav-header":
        monkeypatch.setattr(output.recording.audio_file, "seek", denied)
    else:
        monkeypatch.setattr(Path, "replace", denied)
    # WHEN: сессия завершается; повторная очистка не должна пытаться сохранить архив заново.
    if terminal == "error":
        await output.error("Ошибка модели", "Полученный текст.")
    elif terminal == "disconnected":
        output.close("disconnected", "Полученный текст.")
    else:
        await output.send({"type": terminal, "text": "Полученный текст."})
    output.close("disconnected", "")
    # THEN: нет ложного пути сохранения и повторов; при живом соединении ошибка приходит до итога.
    assert len(attempts) == 1
    assert output.recording.closed and output.recording.audio_file.closed and output.recording.events.closed
    assert "Не удалось сохранить аудио: Финализация недоступна" in caplog.text
    errors = [event for event in events if event["type"] == "recording"]
    if terminal == "disconnected":
        assert errors == [] and events[-1]["type"] == "commit"
    else:
        assert len(errors) == 1 and errors[0]["message"] == "Финализация недоступна"
        assert events[-2] == errors[0] and events[-1]["type"] == terminal
        assert "recording_path" not in events[-1]
        if terminal == "error":
            assert events[-1]["message"] == "Ошибка модели"
        else:
            assert events[-1]["text"] == "Полученный текст."


@pytest.mark.parametrize("stage", ["creation", "pcm", "journal", "finalization"])
async def test_non_io_archive_error_is_not_treated_as_optional_degradation(tmp_path, monkeypatch, stage):
    # GIVEN: ошибка программы, не относящаяся к разрешённым файловым OSError.
    async def send(message):
        pass

    def invalid(*args):
        raise ValueError("Ошибка контракта архива")

    if stage == "creation":
        monkeypatch.setattr(session_output, "SessionRecording", invalid)
        # WHEN / THEN: создание архива не превращает неизвестную ошибку в успешный handshake.
        with pytest.raises(ValueError, match="Ошибка контракта архива"):
            SessionOutput(Settings(save_recordings=True), "session", send, tmp_path)
        return
    output = SessionOutput(Settings(save_recordings=True), "session", send, tmp_path)
    try:
        with monkeypatch.context() as boundary:
            if stage == "pcm":
                boundary.setattr(output.recording.audio, "writeframesraw", invalid)
            elif stage == "journal":
                boundary.setattr(output.recording.events, "write", invalid)
            else:
                boundary.setattr(Path, "replace", invalid)
            # WHEN / THEN: ошибка распространяется вызывающему коду, не включает режим деградации.
            with pytest.raises(ValueError, match="Ошибка контракта архива"):
                if stage == "pcm":
                    await output.append(b"\x01\x00")
                elif stage == "journal":
                    await output.send({"type": "commit", "seq": 1, "delta": "Текст."})
                else:
                    await output.send({"type": "session_end", "text": "Текст."})
        assert output.recording.error == ""
    finally:
        output.close("error", "")
