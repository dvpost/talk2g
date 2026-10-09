import pytest

from talk2g.model import Word, words_from_tokens
from talk2g.transcript import Transcript


def test_independent_blocks_are_committed_whole_and_preserve_real_repetitions():
    transcript = Transcript()
    assert transcript.commit_block([Word("Да,", 0, 0.2), Word("да.", 0.3, 0.5)]) == "Да, да."
    assert transcript.commit_block([]) == "" and transcript.sequence == 1
    assert transcript.commit_block([Word("Да,", 4, 4.2), Word("да.", 4.3, 4.5)]) == " Да, да."
    assert transcript.text == "Да, да. Да, да."
    assert transcript.sequence == 2 and transcript.frontier == 4.5


def test_complete_blocks_preserve_punctuation_and_all_words():
    # GIVEN: два независимых результата модели, включая короткое слово и пунктуацию.
    transcript = Transcript()
    # WHEN: модель возвращает целые блоки по очереди.
    first = transcript.commit_block([Word("Привет", 0, 0.3), Word(",", 0.3, 0.4)])
    second = transcript.commit_block([Word("я", 4, 4.1), Word("здесь.", 4.1, 4.5)])
    # THEN: ничего не удерживается для будущих гипотез, пробелы и итог согласованы.
    assert first == "Привет," and second == " я здесь."
    assert transcript.text == first + second == "Привет, я здесь."


def test_subwords_and_space_tokens_produce_words_with_real_times():
    words = words_from_tokens([" При", "вет", ",", " ", "ми", "р", "."], [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6], 1)
    assert [w.text for w in words] == ["Привет,", "мир."]
    assert words[0].start == 0
    assert words[1].start == 0.4
    assert words[1].end == pytest.approx(0.64)
