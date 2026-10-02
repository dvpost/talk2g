"""Local Russian language likelihood; rank supplied ASR text without generating new words."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import project_home

LM_REPOSITORY = "onnx-community/rugpt3small_based_on_gpt2-ONNX"
LM_REVISION = "90cff9a5ab6afbf331c6b73516fb7346b9330da2"
LM_DIRECTORY = "rugpt3small-int8"
LM_FILES = ("onnx/model_int8.onnx", "tokenizer.json", "config.json", "README.md")


def prepare_language_model(home: Path | None = None, *, download: bool = True) -> Path:
    directory = (home or project_home()) / "models" / LM_DIRECTORY
    if all((directory / name).is_file() for name in LM_FILES):
        return directory
    if not download:
        raise RuntimeError(
            "ruGPT3-small ещё не скачана. Выполните download --language-model до включения сравнения"
        )
    from huggingface_hub import snapshot_download

    snapshot_download(LM_REPOSITORY, revision=LM_REVISION, local_dir=directory, allow_patterns=list(LM_FILES))
    return directory


@dataclass(frozen=True)
class Ranking:
    choice: int | None
    scores: tuple[float, float]
    seconds: float
    reason: str


class RussianLanguageModel:
    """Single batched, teacher-forced INT8 forward pass, with identical left context."""

    def __init__(self, home: Path | None = None, *, threads: int = 2, max_tokens: int = 256):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        directory = prepare_language_model(home, download=False)
        config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        self.tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self.bos = config["bos_token_id"]
        self.pad = config["pad_token_id"]
        self.max_tokens = min(max_tokens, config["n_positions"])
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        self.session = ort.InferenceSession(
            str(directory / "onnx/model_int8.onnx"),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        self.cache_shape = (2, config["n_head"], 0, config["n_embd"] // config["n_head"])

    def rank(self, first: str, second: str, *, context: str = "", margin: float = 0.12) -> Ranking:
        began = time.monotonic()
        candidates = [
            self.tokenizer.encode((" " if context else "") + text.lstrip(), add_special_tokens=False).ids
            for text in (first, second)
        ]
        longest = max(map(len, candidates))
        if not min(map(len, candidates)) or longest + 1 > self.max_tokens:
            return Ranking(None, (0.0, 0.0), time.monotonic() - began, "token_budget")
        context_ids = self.tokenizer.encode(context.rstrip(), add_special_tokens=False).ids if context else []
        budget = min(64, self.max_tokens - longest - 1)
        prefix = [self.bos] + (context_ids[-budget:] if budget else [])
        sequences = [prefix + tokens for tokens in candidates]
        size = max(map(len, sequences))
        ids = np.full((2, size), self.pad, dtype=np.int64)
        mask = np.zeros((2, size), dtype=np.int64)
        for index, sequence in enumerate(sequences):
            ids[index, : len(sequence)] = sequence
            mask[index, : len(sequence)] = 1
        inputs = {
            "input_ids": ids,
            "attention_mask": mask,
            "position_ids": np.maximum(np.cumsum(mask, axis=1) - 1, 0),
        }
        for item in self.session.get_inputs():
            if item.name.startswith("past_key_values."):
                inputs[item.name] = np.empty(self.cache_shape, dtype=np.float32)
        logits = self.session.run(["logits"], inputs)[0]
        scores = []
        for index, tokens in enumerate(candidates):
            rows = logits[index, len(prefix) - 1 : len(prefix) + len(tokens) - 1].astype(np.float64)
            maximum = rows.max(axis=1)
            log_z = maximum + np.log(np.exp(rows - maximum[:, None]).sum(axis=1))
            actual = rows[np.arange(len(tokens)), tokens]
            scores.append(float(np.mean(actual - log_z)))
        difference = scores[0] - scores[1]
        choice = (0 if difference > 0 else 1) if abs(difference) >= margin else None
        return Ranking(
            choice, tuple(scores), time.monotonic() - began, "selected" if choice is not None else "tie"
        )
