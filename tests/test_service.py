import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from talk2g import service
from talk2g.config import Settings


@pytest.fixture
def endpoint():
    state = SimpleNamespace(status=200, body=b"", requests=[])

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state.requests.append(self.path)
            self.send_response(state.status)
            self.end_headers()
            self.wfile.write(state.body)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        state.url = f"ws://127.0.0.1:{server.server_port}/v1/dictate"
        try:
            yield state
        finally:
            server.shutdown()
            thread.join()


@pytest.fixture
def launches(monkeypatch):
    launches = []

    def start(command, **options):
        launches.append((command, options))
        return SimpleNamespace()

    monkeypatch.setattr(service.subprocess, "Popen", start)
    return launches


@pytest.mark.parametrize("ready", [False, True], ids=["loading", "ready"])
def test_existing_talk2g_server_is_used_without_spawning_another(endpoint, launches, tmp_path, ready):
    # GIVEN: реальный HTTP health сервера talk2g в процессе загрузки или готового.
    endpoint.body = json.dumps({"app": "talk2g", "protocol": 1, "ready": ready}).encode()
    manager = service.LocalService(Settings(server_url=endpoint.url), tmp_path)
    # WHEN: приложение запрашивает запуск локального сервера.
    manager.start()
    # THEN: существующий сервер не заменяется новым subprocess.
    assert endpoint.requests == ["/health"]
    assert launches == [] and manager.process is None


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        pytest.param(503, b"unavailable", HTTPError, id="http-error"),
        pytest.param(200, b"not JSON", ValueError, id="invalid-json"),
        pytest.param(200, b'{"app":"other","protocol":1}', ValueError, id="other-service"),
        pytest.param(200, b'{"app":"talk2g","protocol":2}', ValueError, id="wrong-protocol"),
    ],
)
def test_failed_health_does_not_start_a_replacement_server(endpoint, launches, tmp_path, status, body, error):
    # GIVEN: доступный endpoint с явной ошибкой или несовместимым ответом.
    endpoint.status, endpoint.body = status, body
    manager = service.LocalService(Settings(server_url=endpoint.url), tmp_path)
    # WHEN: приложение пытается использовать сервер.
    with pytest.raises(error):
        manager.start()
    # THEN: ошибка видна, health вызван один раз, запасной процесс и лог не создаются.
    assert endpoint.requests == ["/health"] and launches == []
    assert not tmp_path.joinpath(".data").exists()


@pytest.mark.parametrize(
    "reason",
    [
        pytest.param(TimeoutError("timeout"), id="timeout"),
        pytest.param(OSError("network failure"), id="network-error"),
    ],
)
def test_network_failure_does_not_start_a_replacement_server(monkeypatch, launches, tmp_path, reason):
    # GIVEN: сбой проверки существующего сервера, отличный от отсутствующего listener.
    def failed(url):
        raise URLError(reason)

    monkeypatch.setattr(service, "health", failed)
    manager = service.LocalService(Settings(), tmp_path)
    # WHEN: запускаем службу.
    with pytest.raises(URLError):
        manager.start()
    # THEN: ошибка сохраняется, другой сервер не запускается.
    assert launches == [] and manager.process is None


def test_absent_local_listener_starts_one_owned_process(monkeypatch, launches, tmp_path):
    # GIVEN: локальный TCP listener отсутствует.
    def absent(url):
        raise URLError(ConnectionRefusedError("refused"))

    monkeypatch.setattr(service, "health", absent)
    manager = service.LocalService(Settings(), tmp_path)
    try:
        # WHEN: приложение запускает свою службу.
        manager.start()
        # THEN: создаётся ровно один процесс с настроенной моделью и портом.
        assert len(launches) == 1
        command, options = launches[0]
        assert command[-2:] == ["--model", "gigaam-v3-e2e-ctc"]
        assert command[command.index("--port") + 1] == "8769"
        assert options["cwd"] == tmp_path
    finally:
        if manager.log_file:
            manager.log_file.close()
