import pytest

from talk2g.delivery import Delivery
from talk2g.dictated_text import DictatedText


def test_duplicate_commit_is_delivered_only_once():
    # GIVEN: уже полученный первый фрагмент.
    delivery = Delivery()
    delivery.accept({"seq": 1, "delta": "Привет"})
    # WHEN: сервер повторяет фрагмент, затем присылает следующий.
    duplicate = delivery.accept({"seq": 1, "delta": "Привет"})
    following = delivery.accept({"seq": 2, "delta": ", мир."})
    # THEN: в тексте нет дубля, следующий фрагмент добавлен ровно один раз.
    assert (duplicate, following, delivery.text, delivery.sequence) == ("", ", мир.", "Привет, мир.", 2)


@pytest.mark.parametrize(
    ("event", "message"),
    [
        pytest.param({"seq": True, "delta": "Лишнее"}, "Неверный номер", id="boolean-sequence"),
        pytest.param({"seq": 0, "delta": "Лишнее"}, "Неверный номер", id="zero-sequence"),
        pytest.param({"seq": 3, "delta": "Лишнее"}, "Пропущен", id="missing-commit"),
        pytest.param({"seq": 2, "delta": None}, "Неверный текст", id="non-text-delta"),
    ],
)
def test_invalid_commit_keeps_last_valid_text_and_sequence(event, message):
    # GIVEN: один корректный фрагмент уже доставлен.
    delivery = Delivery()
    delivery.accept({"seq": 1, "delta": "Сохранённый текст."})
    # WHEN: приходит некорректный следующий фрагмент.
    with pytest.raises(ValueError, match=message):
        delivery.accept(event)
    # THEN: ошибка не изменяет полученный текст и номер последнего фрагмента.
    assert (delivery.text, delivery.sequence) == ("Сохранённый текст.", 1)


def test_split_voice_command_stops_once_but_raw_result_still_matches_server():
    # GIVEN: текст до команды остановки.
    text = DictatedText()
    text.accept({"seq": 1, "delta": "Первый абзац."}, stop_on_phrase=True)
    # WHEN: команда приходит частями, затем сервер присылает уже записанный хвост.
    held = text.accept({"seq": 2, "delta": " Конец"}, stop_on_phrase=True)
    command = text.accept({"seq": 3, "delta": " связи."}, stop_on_phrase=True)
    tail = text.accept({"seq": 4, "delta": " Лишний хвост."}, stop_on_phrase=True)
    matches = text.reconcile("Первый абзац. Конец связи. Лишний хвост.")
    # THEN: команда и хвост не вставляются, проверка целостности учитывает исходные commits.
    assert (held, command, tail) == (("", False), ("", True), ("", False))
    assert (text.text, matches) == ("Первый абзац.", True)


def test_disabling_voice_command_releases_held_ordinary_word():
    # GIVEN: последнее обычное слово удержано как возможное начало команды.
    text = DictatedText()
    text.accept({"seq": 1, "delta": "Это конец"}, stop_on_phrase=True)
    # WHEN: пользователь отключает обработку голосовой команды.
    released = text.flush(stop_on_phrase=False)
    # THEN: слово возвращается в текст одним фрагментом без остановки.
    assert (released, text.text) == ((" конец", False), "Это конец")


def test_finalization_keeps_held_word_and_can_be_repeated_safely():
    # GIVEN: слово «конец» не завершает обычную фразу.
    text = DictatedText()
    text.accept({"seq": 1, "delta": "Это конец"}, stop_on_phrase=True)
    # WHEN: запись завершается горячей клавишей, затем завершается клиентский поток.
    first = text.flush(stop_on_phrase=True, final=True)
    second = text.flush(stop_on_phrase=True, final=True)
    # THEN: обычное слово возвращается в текст, повторное завершение не создаёт дубликат.
    assert (first, second, text.text) == ((" конец", False), ("", False), "Это конец")


@pytest.mark.parametrize("stop_on_phrase", [True, False], ids=["command-enabled", "command-disabled"])
def test_mismatched_server_result_preserves_confirmed_text(stop_on_phrase):
    # GIVEN: часть текста уже могла быть вставлена в другое приложение.
    text = DictatedText()
    text.accept({"seq": 1, "delta": "Начало."}, stop_on_phrase=stop_on_phrase)
    # WHEN: сервер возвращает полный результат, отличающийся от commits.
    matches = text.reconcile("Полный результат.")
    # THEN: ошибка не заменяет уже подтверждённый текст и не запрашивает новую вставку.
    assert (matches, text.text) == (False, "Начало.")
    assert text.flush(stop_on_phrase=stop_on_phrase, final=True) == ("", False)


def test_mismatched_result_does_not_restore_command_after_voice_stop_is_disabled():
    # GIVEN: голосовая команда уже завершила диктовку.
    text = DictatedText()
    text.accept({"seq": 1, "delta": "Начало. Конец связи."}, stop_on_phrase=True)
    # WHEN: обработка команд выключена, а сервер возвращает иной полный текст с командой.
    matches = text.reconcile("Полный текст. Конец связи. Хвост.")
    # THEN: подтверждённый текст сохраняется, хвост после команды не возвращается во ввод.
    assert (matches, text.text) == (False, "Начало.")
    assert text.flush(stop_on_phrase=False, final=True) == ("", False)
