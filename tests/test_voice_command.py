import pytest

from talk2g.voice_command import VoiceStop


@pytest.mark.parametrize(
    "chunks,expected,stop",
    [
        (["Проверка. Конец связи."], "Проверка.", True),
        (["Проверка.", " Конец", " связи."], "Проверка.", True),
        (["Проверка. кон", "ец св", "язи!", " После команды"], "Проверка.", True),
        (["Проверка. КОНЕЦ,\nСВЯЗИ! Ещё речь"], "Проверка.", True),
        (["Конец связи"], "", True),
        (["На этом конец", " недели."], "На этом конец недели.", False),
        (["Конец связистам."], "Конец связистам.", False),
        (["Наконец связи восстановлены."], "Наконец связи восстановлены.", False),
        (["Конец"], "Конец", False),
        (["Обычный текст."], "Обычный текст.", False),
    ],
)
def test_split_command_is_removed_without_losing_ordinary_text(chunks, expected, stop):
    command = VoiceStop()
    results = [command.feed(chunk, enabled=True) for chunk in chunks]
    results.append(command.feed("", enabled=True, final=True))
    assert "".join(delta for delta, _ in results) == expected
    assert sum(triggered for _, triggered in results) == int(stop)
    assert command.triggered == stop


def test_disabling_releases_pending_words_and_retains_literal_command():
    command = VoiceStop()
    assert command.feed("Текст. конец", enabled=True) == ("Текст.", False)
    assert command.feed("", enabled=False) == (" конец", False)
    assert command.feed(" связи.", enabled=False) == (" связи.", False)
    assert not command.triggered


def test_disabling_after_command_cannot_reintroduce_queued_speech():
    command = VoiceStop()
    assert command.feed("Текст. конец связи.", enabled=True) == ("Текст.", True)
    assert command.feed(" Ещё речь.", enabled=False, final=True) == ("", False)
