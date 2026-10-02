"""Combine actual speech, a synthesized command, silence and speech after the command."""

import argparse
import json
from pathlib import Path

import numpy as np
import soundfile as sf

from giga_dictation.audio import read_audio
from giga_dictation.config import RATE


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", type=Path, default=Path("benchmarks/voice-command.wav"))
    parser.add_argument("--output", type=Path, default=Path("benchmarks/voice-stop-input.wav"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    speech = read_audio(root / "tests/fixtures/example.wav")
    command = read_audio(args.command)
    samples = np.concatenate((speech, np.zeros(RATE), command, np.zeros(3 * RATE), speech))
    sf.write(args.output, samples, RATE, subtype="PCM_16")
    metadata = {
        "command": "Конец связи.",
        "command_end_seconds": (len(speech) + RATE + len(command)) / RATE,
        "total_seconds": len(samples) / RATE,
        "tail_speech_starts_seconds": (len(speech) + 4 * RATE + len(command)) / RATE,
        "synthesis": "Piper ru_RU-denis-medium; benchmark fixture only",
    }
    args.output.with_suffix(".json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
    print(json.dumps(metadata, ensure_ascii=False))


if __name__ == "__main__":
    main()
