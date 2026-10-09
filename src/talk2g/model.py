from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from .config import RATE, Settings, project_home

log = logging.getLogger(__name__)
MODEL_REVISION = "322c3b29492673eb7d0b434bfa9dfb8653e34d02"
VAD_REVISION = "b3e3ee3cce4c11ceb63b1a0b229d916069c1ddf6"


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float


class Recognizer(Protocol):
    def decode(self, audio: np.ndarray) -> list[Word]: ...


def words_from_tokens(tokens: list[str], times: list[float], duration: float) -> list[Word]:
    """Keep actual token timing, including repeated words and subword pieces."""
    words: list[Word] = []
    text = ""
    start = 0.0
    end = 0.0
    for token, timestamp in zip(tokens, times, strict=True):
        token = token.replace("▁", " ")
        for piece in token.splitlines() or [token]:
            if piece.startswith(" ") and text:
                words.append(Word(text, start, min(duration, end)))
                text = ""
            if not text:
                start = timestamp
            text += piece.lstrip() if not text else piece
            end = timestamp + 0.04
    if text.strip():
        words.append(Word(text.strip(), start, min(duration, end)))
    return words


def prepare_models(settings: Settings, home: Path | None = None) -> tuple[Path, Path]:
    from huggingface_hub import snapshot_download

    root = (home or project_home()) / "models"
    model_dir = root / (settings.model + "-int8")
    stem = settings.model.removeprefix("gigaam-").replace("-", "_")
    if stem.endswith("ctc"):
        names = [f"{stem}.int8.onnx", f"{stem}_vocab.txt"]
    else:
        names = [f"{stem}_{part}.int8.onnx" for part in ("encoder", "decoder", "joint")]
        names.append(f"{stem}_vocab.txt")
    names += ["config.json", "LICENSE.txt"]
    if not all((model_dir / name).is_file() for name in names):
        log.info("Скачивание %s (один раз), каталог %s", settings.model, model_dir)
        snapshot_download(
            "istupakov/gigaam-v3-onnx",
            revision=MODEL_REVISION,
            local_dir=model_dir,
            allow_patterns=names,
        )
    return model_dir, prepare_vad(home)


def prepare_vad(home: Path | None = None) -> Path:
    from huggingface_hub import snapshot_download

    vad_dir = (home or project_home()) / "models" / "silero-vad"
    if not (vad_dir / "silero_vad.onnx").is_file():
        log.info("Скачивание детектора речи Silero VAD")
        snapshot_download(
            "istupakov/silero-vad-onnx",
            revision=VAD_REVISION,
            local_dir=vad_dir,
            allow_patterns=["silero_vad.onnx", "LICENSE.txt"],
        )
    return vad_dir / "silero_vad.onnx"


class GigaRecognizer:
    def __init__(self, settings: Settings, home: Path | None = None):
        import onnx_asr
        import onnxruntime as ort

        settings.validate()
        model_dir, self.vad_path = prepare_models(settings, home)
        options = ort.SessionOptions()
        options.intra_op_num_threads = settings.threads
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        self.model = onnx_asr.load_model(
            settings.model,
            model_dir,
            quantization="int8",
            sess_options=options,
            providers=["CPUExecutionProvider"],
        ).with_timestamps()
        self.decode(np.zeros(RATE, dtype=np.float32))
        log.info("GigaAM готова: %s, CPU INT8, потоков: %d", settings.model, settings.threads)

    def decode(self, audio: np.ndarray) -> list[Word]:
        if len(audio) < RATE // 10:
            return []
        result = self.model.recognize(np.asarray(audio, dtype=np.float32), sample_rate=RATE)
        if not result.tokens or result.timestamps is None:
            return []
        return words_from_tokens(result.tokens, result.timestamps, len(audio) / RATE)


class SileroDetector:
    def __init__(self, path: Path):
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        self.model = ort.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros(64, dtype=np.float32)

    def probability(self, frame: np.ndarray) -> float:
        if len(frame) != 512:
            raise ValueError("Silero VAD требует кадр из 512 сэмплов")
        signal = np.concatenate((self.context, frame))[None, :].astype(np.float32)
        probability, self.state = self.model.run(
            ["output", "stateN"],
            {"input": signal, "state": self.state, "sr": np.array([RATE], dtype=np.int64)},
        )
        self.context = frame[-64:].copy()
        return float(probability[0, 0])
