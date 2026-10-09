from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

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
        _download_files("istupakov/gigaam-v3-onnx", MODEL_REVISION, model_dir, names)
    return model_dir, prepare_vad(home)


def prepare_vad(home: Path | None = None) -> Path:
    vad_dir = (home or project_home()) / "models" / "silero-vad"
    names = ["silero_vad.onnx", "LICENSE.txt"]
    if not all((vad_dir / name).is_file() for name in names):
        log.info("Скачивание детектора речи Silero VAD")
        _download_files("istupakov/silero-vad-onnx", VAD_REVISION, vad_dir, names)
    return vad_dir / "silero_vad.onnx"


def _download_files(repository: str, revision: str, directory: Path, names: list[str]):
    """Fetch each missing pinned asset once; never publish an incomplete download."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        destination = directory / name
        if destination.is_file():
            continue
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
                temporary = Path(stream.name)
                with urlopen(
                    f"https://huggingface.co/{repository}/resolve/{revision}/{name}", timeout=30
                ) as response:
                    shutil.copyfileobj(response, stream)
                    length = response.headers.get("Content-Length")
                    if length is not None and stream.tell() != int(length):
                        raise OSError(f"Неполная загрузка {name}")
            temporary.replace(destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


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
        onnx_config = {
            "sess_options": options,
            "providers": ["CPUExecutionProvider"],
            "enable_fallback": False,
        }
        self.model = onnx_asr.load_model(
            settings.model,
            model_dir,
            quantization="int8",
            asr_config=onnx_config,
            preprocessor_config={"use_numpy_preprocessors": True},
            resampler_config=onnx_config,
        ).with_timestamps()
        self.decode(np.zeros(RATE, dtype=np.float32))
        log.info("GigaAM готова: %s, CPU INT8, потоков: %d", settings.model, settings.threads)

    def decode(self, audio: np.ndarray) -> list[Word]:
        if len(audio) < RATE // 10:
            return []
        result = self.model.recognize(np.asarray(audio, dtype=np.float32), sample_rate=RATE)
        if not result.tokens:
            return []
        if result.timestamps is None:
            raise ValueError("Модель вернула токены без временных отметок")
        return words_from_tokens(result.tokens, result.timestamps, len(audio) / RATE)


class SileroDetector:
    def __init__(self, path: Path):
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        self.model = ort.InferenceSession(
            str(path), sess_options=options, providers=["CPUExecutionProvider"], enable_fallback=False
        )
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
