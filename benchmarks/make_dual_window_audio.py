"""Prepare public test audio with optional local Piper; no TTS dependency in the application."""

import argparse
import json
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

from giga_dictation.audio import read_audio
from giga_dictation.config import RATE


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", default=".tools/piper-venv/bin/python")
    parser.add_argument("--model", default=".tools/ru_RU-denis-medium.onnx")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    fixtures = root / "tests/fixtures"
    poem = (
        "Ничьих не требуя похвал счастлив уж я надеждой сладкой что дева с трепетом любви "
        "посмотрит может быть украдкой на песни грешные мои У лукоморья дуб зелёный"
    )
    cases = [{"name": "poem", "audio": "tests/fixtures/example.wav", "reference": poem}]
    phrases = {
        "technical": "Нужно перезапустить сервер распознавания речи, проверить очередь сообщений "
        "и сохранить настройки микрофона. После перезапуска программа должна работать без интернета.",
        "repetitions": "Да, да, да. Я хочу проверить повторённые слова. "
        "Нет, нет, речь продолжается. Проверка закончилась.",
        "names_numbers": "Анна Смирнова передала Николаю Петрову документ номер 38. "
        "Встреча назначена на 25 октября. Это важное уточнение.",
    }
    for name, phrase in phrases.items():
        target = fixtures / f"dual-{name}.wav"
        subprocess.run(
            [args.python, "-m", "piper", "-m", args.model, "-f", str(target), "--", phrase], check=True
        )
        cases.append({"name": name, "audio": str(target.relative_to(root)), "reference": phrase})
        if name == "names_numbers":
            cases[-1]["aliases"] = {"38": "тридцать восемь", "25": "двадцать пять"}
    speech = read_audio(fixtures / "example.wav")
    sf.write(fixtures / "dual-long.wav", np.tile(speech, 3), RATE, subtype="PCM_16")
    cases.append(
        {"name": "long_overlap", "audio": "tests/fixtures/dual-long.wav", "reference": " ".join([poem] * 3)}
    )
    (fixtures / "dual-window-cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            [{k: v for k, v in case.items() if k != "reference"} for case in cases], ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
