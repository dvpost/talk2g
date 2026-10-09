"""Reproducible CPU comparison on the same real Russian recording."""

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np

from talk2g.audio import read_audio
from talk2g.config import RATE, Settings
from talk2g.model import GigaRecognizer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", default="tests/fixtures/example.wav")
    parser.add_argument("--output", default="benchmarks/model-results.json")
    args = parser.parse_args()
    audio = read_audio(args.audio)
    results = []
    for name in ("gigaam-v3-e2e-ctc", "gigaam-v3-e2e-rnnt"):
        recognizer = GigaRecognizer(Settings(model=name))
        for duration in (2, 4, 8, len(audio) / RATE):
            window = audio[: round(duration * RATE)]
            times = []
            for _ in range(3):
                start = time.perf_counter()
                words = recognizer.decode(window)
                times.append(time.perf_counter() - start)
            results.append(
                {
                    "model": name,
                    "audio_seconds": duration,
                    "decode_median": float(np.median(times)),
                    "text": " ".join(w.text for w in words),
                    "words": [asdict(w) for w in words],
                }
            )
        del recognizer
    Path(args.output).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    for result in results:
        print(result["model"], result["audio_seconds"], round(result["decode_median"], 3), result["text"])


if __name__ == "__main__":
    main()
