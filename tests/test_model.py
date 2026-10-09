import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import urlopen

import numpy as np
import pytest

from talk2g import model
from talk2g.config import RATE, Settings


@pytest.fixture
def downloads(monkeypatch):
    state = SimpleNamespace(status=200, body=b"asset bytes", length=None, urls=[])

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(state.status)
            self.send_header(
                "Content-Length", str(state.length if state.length is not None else len(state.body))
            )
            self.end_headers()
            self.wfile.write(state.body)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()

        def fetch(url, *, timeout):
            state.urls.append(url)
            assert timeout == 30
            return urlopen(f"http://127.0.0.1:{server.server_port}/asset", timeout=timeout)

        monkeypatch.setattr(model, "urlopen", fetch)
        try:
            yield state
        finally:
            server.shutdown()
            thread.join()


@pytest.mark.parametrize(
    ("name", "assets"),
    [
        pytest.param("gigaam-v3-e2e-ctc", ["v3_e2e_ctc.int8.onnx", "v3_e2e_ctc_vocab.txt"], id="ctc"),
        pytest.param(
            "gigaam-v3-e2e-rnnt",
            [
                "v3_e2e_rnnt_encoder.int8.onnx",
                "v3_e2e_rnnt_decoder.int8.onnx",
                "v3_e2e_rnnt_joint.int8.onnx",
                "v3_e2e_rnnt_vocab.txt",
            ],
            id="rnnt",
        ),
    ],
)
def test_missing_pinned_assets_download_once_and_are_reused_offline(downloads, tmp_path, name, assets):
    # GIVEN: пустой каталог весов и реальные HTTP-ответы для каждого недостающего файла.
    settings = Settings(model=name)
    # WHEN: готовим модель дважды.
    directory, vad = model.prepare_models(settings, tmp_path)
    repeated = model.prepare_models(settings, tmp_path)
    # THEN: каждый файл скачан один раз с закреплённой ревизией; второй запуск офлайн.
    assert repeated == (directory, vad)
    assert downloads.urls == [
        *(
            f"https://huggingface.co/istupakov/gigaam-v3-onnx/resolve/{model.MODEL_REVISION}/{asset}"
            for asset in [*assets, "config.json", "LICENSE.txt"]
        ),
        *(
            f"https://huggingface.co/istupakov/silero-vad-onnx/resolve/{model.VAD_REVISION}/{asset}"
            for asset in ["silero_vad.onnx", "LICENSE.txt"]
        ),
    ]
    assert {path.name for path in directory.iterdir()} == set(assets) | {"config.json", "LICENSE.txt"}
    assert all(path.read_bytes() == downloads.body for path in directory.iterdir())
    assert vad.read_bytes() == downloads.body


@pytest.mark.parametrize("status", [429, 500, 503], ids=["rate-limit", "server-error", "unavailable"])
def test_failed_download_is_not_retried_and_does_not_publish_partial_weights(downloads, tmp_path, status):
    # GIVEN: сервер весов возвращает ошибку, ранее сохранённый словарь уже существует.
    downloads.status = status
    directory = tmp_path / "models/gigaam-v3-e2e-ctc-int8"
    directory.mkdir(parents=True)
    vocabulary = directory / "v3_e2e_ctc_vocab.txt"
    vocabulary.write_bytes(b"existing vocabulary")
    # WHEN: загружаем недостающую модель.
    with pytest.raises(HTTPError) as error:
        model.prepare_models(Settings(), tmp_path)
    # THEN: ровно один запрос без SDK retry/cache fallback, временный файл удалён.
    assert error.value.code == status and len(downloads.urls) == 1
    assert list(directory.iterdir()) == [vocabulary]
    assert vocabulary.read_bytes() == b"existing vocabulary"
    assert not tmp_path.joinpath("models/silero-vad").exists()


def test_truncated_download_is_rejected_without_retry(downloads, tmp_path):
    # GIVEN: ответ завершается раньше объявленного размера файла.
    downloads.length = len(downloads.body) + 10
    # WHEN: готовим VAD.
    with pytest.raises(OSError, match="Неполная загрузка"):
        model.prepare_vad(tmp_path)
    # THEN: один запрос, повреждённый файл не становится действующей моделью.
    assert len(downloads.urls) == 1
    assert list(tmp_path.joinpath("models/silero-vad").iterdir()) == []


@pytest.fixture
def recognition_result(monkeypatch, tmp_path):
    import onnx_asr

    def create(*, tokens, timestamps):
        adapter = SimpleNamespace(
            recognize=lambda *args, **kwargs: SimpleNamespace(tokens=tokens, timestamps=timestamps)
        )
        monkeypatch.setattr(
            onnx_asr, "load_model", lambda *args, **kwargs: SimpleNamespace(with_timestamps=lambda: adapter)
        )
        monkeypatch.setattr(model, "prepare_models", lambda *args: (tmp_path, tmp_path / "vad.onnx"))
        return model.GigaRecognizer(Settings(), tmp_path)

    return create


def test_tokens_without_timestamps_are_not_silently_treated_as_silence(recognition_result):
    # GIVEN: внешний ONNX-adapter возвращает непустой текст без временных отметок.
    # WHEN: создаём распознаватель, выполняющий прогрев через decode.
    with pytest.raises(ValueError, match="без временных отметок"):
        recognition_result(tokens=[" Слово"], timestamps=None)
    # THEN: неполный результат вызывает явную ошибку.


def test_silent_recognition_result_without_timestamps_returns_no_words(recognition_result):
    # GIVEN: ONNX-adapter подтверждает отсутствие распознанных токенов.
    recognizer = recognition_result(tokens=[], timestamps=None)
    # WHEN: распознаём тишину через публичный интерфейс.
    words = recognizer.decode(np.zeros(RATE))
    # THEN: пустой результат допустим без временных отметок.
    assert words == []


@pytest.mark.parametrize("kind", ["asr", "vad"], ids=["gigaam", "silero"])
@pytest.mark.parametrize(
    "stage", ["initialization", "run"], ids=["provider-init-fails", "provider-run-fails"]
)
def test_onnx_provider_failure_is_propagated_without_runtime_retry(monkeypatch, tmp_path, kind, stage):
    # GIVEN: настоящий Python ONNX Runtime с отказом на внешней C++-границе.
    import onnx_asr
    import onnxruntime as ort
    from onnxruntime.capi import _pybind_state as native

    asset = files("onnx_asr.preprocessors").joinpath("data/resample_8_16.onnx").read_bytes()
    original_session = ort.InferenceSession
    original_native = native.InferenceSession
    creations, runs = [], []

    class FailedNativeSession:
        def __init__(self, *args):
            creations.append(True)
            self.session = original_native(*args)

        def __getattr__(self, name):
            return getattr(self.session, name)

        def run(self, *args):
            runs.append(True)
            raise native.EPFail("provider run failed")

        def initialize_session(self, *args):
            self.session.initialize_session(*args)
            if stage == "initialization":
                raise RuntimeError("provider initialization failed")

        @property
        def inputs_meta(self):
            if kind == "vad":
                return [SimpleNamespace(name=name) for name in ("input", "state", "sr")]
            return self.session.inputs_meta

    monkeypatch.setattr(native, "InferenceSession", FailedNativeSession)

    if kind == "asr":

        def load(*args, **kwargs):
            session = original_session(asset, **kwargs.get("asr_config", {}))

            def recognize(*args, **kwargs):
                session.run(
                    None,
                    {
                        "waveforms": np.zeros((1, 8000), dtype=np.float32),
                        "waveforms_lens": np.array([8000], dtype=np.int64),
                    },
                )
                return SimpleNamespace(tokens=[], timestamps=[])

            adapter = SimpleNamespace(recognize=recognize)
            return SimpleNamespace(with_timestamps=lambda: adapter)

        monkeypatch.setattr(onnx_asr, "load_model", load)
        monkeypatch.setattr(model, "prepare_models", lambda *args: (tmp_path, tmp_path / "vad.onnx"))
    else:
        monkeypatch.setattr(ort, "InferenceSession", lambda path, **kwargs: original_session(asset, **kwargs))

    # WHEN: создаём распознаватель/детектор и вызываем их публичный интерфейс.
    error, message = (
        (RuntimeError, "initialization failed")
        if stage == "initialization"
        else (native.EPFail, "run failed")
    )
    with pytest.raises(error, match=message):
        if kind == "asr":
            model.GigaRecognizer(Settings(), tmp_path)
        else:
            model.SileroDetector(tmp_path / "vad.onnx").probability(np.zeros(512))
    # THEN: исходная ошибка проброшена после одной попытки, ONNX не пересоздаёт сессию для retry.
    assert creations == [True]
    assert runs == ([] if stage == "initialization" else [True])
