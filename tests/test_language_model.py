import os
from types import SimpleNamespace

import pytest

from giga_dictation.language_model import RussianLanguageModel


def test_overlong_candidates_abstain_before_native_model_is_called():
    model = RussianLanguageModel.__new__(RussianLanguageModel)
    model.max_tokens = 4
    model.tokenizer = SimpleNamespace(
        encode=lambda text, **kwargs: SimpleNamespace(ids=list(range(len(text))))
    )
    result = model.rank("слишком длинно", "коротко")
    assert result.choice is None and result.reason == "token_budget"


@pytest.mark.real_model
@pytest.mark.skipif(not os.environ.get("GIGA_LM_TESTS"), reason="Set GIGA_LM_TESTS=1 with downloaded ruGPT")
def test_real_russian_model_prefers_correct_grammar_independent_of_candidate_position():
    model = RussianLanguageModel()
    pairs = [
        ("Мы исправили код приложения и запустили тесты.", "Мы исправили кот приложения и запустили тесты."),
        ("Я хочу включить автоматическую вставку текста.", "Я хочу включить автоматическую вставка текста."),
        ("Сохрани изменения в отдельной ветке.", "Сохрани изменения в отдельной ветки."),
    ]
    for good, bad in pairs:
        first = model.rank(good, bad, context="Мы обсуждаем работу программы.")
        swapped = model.rank(bad, good, context="Мы обсуждаем работу программы.")
        assert first.choice == 0 and swapped.choice == 1
        assert first.scores == pytest.approx(tuple(reversed(swapped.scores)), abs=1e-5)
    # Even with a very long committed context, the candidates and BOS must fit
    # the budget together. This also exercises right-padding with unequal lengths.
    longest = max(len(model.tokenizer.encode(" " + text, add_special_tokens=False).ids) for text in pairs[0])
    model.max_tokens = longest + 1
    assert model.rank(*pairs[0], context="Предыдущий текст. " * 100).choice == 0
