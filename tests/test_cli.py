import asyncio
import json
from types import SimpleNamespace

import numpy as np
import pytest
from websockets.asyncio.server import serve

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
    text = cli.transcribe("test.wav", Settings(recognition_pause=pause))
    assert len(inputs) == blocks
    assert text == " ".join(f"Блок{number}." for number in range(1, blocks + 1))
    expected_tail = np.frombuffer(pcm(audio[-207:]), dtype="<i2").astype(np.float32) / 32768
    np.testing.assert_array_equal(inputs[-1][-207:], expected_tail)


async def test_replay_rejects_invalid_ready_before_audio(monkeypatch):
    # GIVEN: сервер возвращает неверный тип пути записи в ready.
    monkeypatch.setattr(cli, "read_audio", lambda path: np.zeros(1600))
    received = []

    async def handler(socket):
        received.append(json.loads(await socket.recv()))
        await socket.send('{"type":"ready","recognize_on_pause":true,"recording_path":7}')
        async for packet in socket:
            received.append(packet)
            if isinstance(packet, str):
                await socket.send('{"type":"session_end","text":""}')

    async with serve(handler, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        # WHEN: запускаем CLI replay через настоящий транспорт.
        with pytest.raises(ValueError, match="Некорректный ответ сервера:.*recording_path"):
            await asyncio.wait_for(cli.replay("test.wav", url, speed=0), 3)
    # THEN: подтверждение отвергнуто до передачи PCM.
    assert len(received) == 1 and received[0]["type"] == "start"


async def test_replay_preserves_printed_commit_and_journal_on_invalid_terminal(monkeypatch, tmp_path, capsys):
    # GIVEN: сервер сначала передаёт commit, затем итог неверного типа.
    monkeypatch.setattr(cli, "read_audio", lambda path: np.zeros(1600))
    journal = tmp_path / "events.json"

    async def handler(socket):
        await socket.recv()
        await socket.send('{"type":"ready","recognize_on_pause":true}')
        async for packet in socket:
            if isinstance(packet, str):
                await socket.send('{"type":"commit","seq":1,"delta":"Подтверждено."}')
                await socket.send('{"type":"session_end","text":7}')
                break

    async with serve(handler, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        # WHEN: CLI получает некорректный итог.
        with pytest.raises(ValueError, match="Некорректный ответ сервера:.*text"):
            await asyncio.wait_for(cli.replay("test.wav", url, speed=0, events_path=str(journal)), 3)
    # THEN: текст напечатан один раз; журнал содержит только корректное событие и время приёма.
    assert capsys.readouterr().out == "Подтверждено."
    events = json.loads(journal.read_text())
    assert len(events) == 1
    assert events[0] == {
        "type": "commit",
        "seq": 1,
        "delta": "Подтверждено.",
        "received_seconds": events[0]["received_seconds"],
    }
    assert type(events[0]["received_seconds"]) is float and events[0]["received_seconds"] >= 0
