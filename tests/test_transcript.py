import pytest

from talk2g.client import Delivery
from talk2g.model import Word, words_from_tokens
from talk2g.transcript import Transcript


def test_independent_blocks_are_committed_whole_and_preserve_real_repetitions():
    transcript = Transcript()
    assert transcript.commit_block([Word("Да,", 0, 0.2), Word("да.", 0.3, 0.5)]) == "Да, да."
    assert transcript.commit_block([]) == "" and transcript.sequence == 1
    assert transcript.commit_block([Word("Да,", 4, 4.2), Word("да.", 4.3, 4.5)]) == " Да, да."
    assert transcript.text == "Да, да. Да, да."
    assert transcript.sequence == 2 and transcript.frontier == 4.5


def test_unstable_tail_is_revised_but_inserted_prefix_is_not():
    transcript = Transcript(0.4)
    old = [Word("Завтра", 0, 0.2), Word("пошлю", 0.5, 0.7), Word("отчёт", 1.1, 1.3)]
    assert transcript.update(old, 1.4)[0] == ""
    revised = [Word("Завтра", 0, 0.2), Word("отправлю", 0.5, 0.7), Word("документы", 1.1, 1.3)]
    delta, partial = transcript.update(revised, 1.4)
    assert delta == "Завтра"
    assert partial == "отправлю документы"
    assert transcript.update(revised, 1.9)[0] == " отправлю"
    assert transcript.update(revised, 2.0, final=True)[0] == " документы"
    assert transcript.text == "Завтра отправлю документы"


def test_repeated_words_survive_overlap_without_duplicates():
    transcript = Transcript(0.2)
    words = [Word("Да,", 0, 0.1), Word("да,", 0.4, 0.5), Word("да.", 1.0, 1.1)]
    transcript.update(words, 2)
    assert transcript.update(words, 2)[0] == "Да, да,"
    assert transcript.update(words, 2, final=True)[0] == " да."
    assert transcript.update(words, 2, final=True)[0] == ""
    assert transcript.text == "Да, да, да."


def test_matching_phrase_farther_in_time_is_new_speech():
    transcript = Transcript()
    transcript.update([Word("Да", 0, 0.1)], 1, final=True)
    assert transcript.update([Word("Да", 4, 4.1)], 5, final=True)[0] == " Да"


def test_temporal_frontier_is_used_when_old_word_changes():
    transcript = Transcript()
    transcript.update([Word("кошка", 0, 0.3)], 1, final=True)
    assert transcript.update([Word("кошки", 0, 0.3), Word("спят", 0.6, 0.9)], 2, final=True)[0] == " спят"


def test_short_adjacent_word_survives_when_previous_anchor_changes():
    transcript = Transcript()
    transcript.update([Word("мои.", 0, 0.3)], 1, final=True)
    revised = [Word("моё.", 0, 0.3), Word("У", 0.3, 0.38), Word("лукоморья", 0.4, 0.7)]
    assert transcript.update(revised, 1, final=True)[0] == " У лукоморья"


def test_forced_window_cut_retains_incomplete_word_for_overlap():
    transcript = Transcript(0.8)
    words = [Word("Первое", 0, 0.3), Word("второе", 1, 1.3), Word("послед", 2, 2.1)]
    assert transcript.update(words, 2.5, final=True, forced=True)[0] == "Первое второе"
    assert (
        transcript.update([Word("второе", 1, 1.3), Word("последнее.", 2, 2.4)], 3, final=True)[0]
        == " последнее."
    )


def test_subwords_and_space_tokens_produce_words_with_real_times():
    words = words_from_tokens([" При", "вет", ",", " ", "ми", "р", "."], [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6], 1)
    assert [w.text for w in words] == ["Привет,", "мир."]
    assert words[0].start == 0
    assert words[1].start == 0.4
    assert words[1].end == pytest.approx(0.64)


def test_delivery_deduplicates_events_and_refuses_missing_delta():
    delivery = Delivery()
    event = {"seq": 1, "delta": "Привет"}
    assert delivery.accept(event) == "Привет"
    assert delivery.accept(event) == ""
    with pytest.raises(ValueError, match="Пропущен"):
        delivery.accept({"seq": 3, "delta": " мир"})
    assert delivery.text == "Привет"
