"""Measure actual Russian model preferences and CPU latency on fixed contrast pairs."""

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path

from giga_dictation.language_model import RussianLanguageModel

PAIRS = [
    ("Мы исправили код приложения и запустили тесты.", "Мы исправили кот приложения и запустили тесты."),
    ("Пожалуйста, открой окно настроек.", "Пожалуйста, открой окна настроек."),
    ("Я хочу включить автоматическую вставку текста.", "Я хочу включить автоматическую вставка текста."),
    ("Вчера мы проверили новую модель.", "Вчера мы проверил новую модель."),
    ("Сохрани изменения в отдельной ветке.", "Сохрани изменения в отдельной ветки."),
    ("Модель распознаёт речь на русском языке.", "Модель распознаёт речь на русский языке."),
    ("У меня в буфере обмена остался весь текст.", "У меня в буфере обмена остался вес текст."),
    ("Это работает только со второго раза.", "Это работает только со второго роза."),
    ("Я встретил Даниила возле офиса.", "Я встретил Данила возле офиса."),
    ("Запиши двадцать пять повторов.", "Запиши двадцать пять повторов повторов."),
]


def main(output):
    began = time.monotonic()
    model = RussianLanguageModel()
    loaded = time.monotonic() - began
    rows = []
    for index, (first, second) in enumerate(PAIRS):
        # Alternate positions so a fixed preference for the first input cannot pass.
        candidates = (first, second) if index % 2 == 0 else (second, first)
        ranking = model.rank(*candidates)
        row = {"texts": candidates, "expected": index % 2 if index != 8 else None, **asdict(ranking)}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    result = {"load_seconds": loaded, "pairs": rows}
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("benchmarks/russian-lm-text-results.json"))
    main(parser.parse_args().output)
