import json
from dataclasses import asdict

import pytest

from talk2g.config import Settings
from talk2g.protocol import make_start, read_server_event, read_start, session_settings


def test_start_message_preserves_protocol_and_all_session_options():
    # GIVEN: клиент с изменённой паузой распознавания и сохранением аудио.
    settings = Settings(
        token="секрет",
        recognition_pause=5,
        save_recordings=True,
    )
    # WHEN: клиент формирует сообщение начала сессии.
    start = make_start(settings, "example-session")
    # THEN: сообщение содержит полный контракт v1 и не передаёт настройки интерфейса.
    assert start == {
        "type": "start",
        "version": 1,
        "format": "pcm16",
        "rate": 16000,
        "session_id": "example-session",
        "token": "секрет",
        "save_recordings": True,
        "recognize_on_pause": True,
        "recognition_pause": 5,
    }


@pytest.mark.parametrize(
    "message",
    [
        pytest.param(b"{}", id="binary-handshake"),
        pytest.param("[]", id="non-object"),
        pytest.param('{"type":"stop","version":1}', id="wrong-message"),
        pytest.param('{"type":"start","version":true}', id="boolean-version"),
        pytest.param('{"type":"start","version":2}', id="unknown-version"),
        pytest.param('{"type":"start","version":1,"rate":8000,"format":"pcm16"}', id="wrong-rate"),
        pytest.param('{"type":"start","version":1,"rate":16000,"format":"float32"}', id="wrong-format"),
    ],
)
def test_invalid_start_is_rejected(message):
    # GIVEN: сервер с настройками по умолчанию.
    settings = Settings()
    # WHEN: клиент отправляет несовместимое начало сессии.
    with pytest.raises(ValueError):
        read_start(message, settings)
    # THEN: ни одна настройка сервера не изменена.
    assert settings == Settings()


@pytest.mark.parametrize("token", ["", "wrong", "сéкрет"], ids=["missing", "incorrect", "unicode"])
def test_authentication_rejects_wrong_token(token):
    # GIVEN: сервер требует токен, транспорт и версия протокола корректны.
    message = '{"type":"start","version":1,"rate":16000,"format":"pcm16","token":' + json.dumps(token) + "}"
    # WHEN: клиент передаёт неподходящий токен.
    with pytest.raises(ValueError, match="Неверный токен"):
        read_start(message, Settings(token="секрет"))
    # THEN: отказ происходит до создания сессии распознавания.


def test_session_overrides_do_not_change_server_defaults_or_other_clients_settings():
    # GIVEN: серверный токен и клиентские попытки изменить поля вне сессии.
    defaults = Settings(token="server-secret", threads=8)
    original = asdict(defaults)
    start = make_start(Settings(token="server-secret", recognition_pause=5, save_recordings=True), "session")
    start.update(threads=1, model="invalid", auto_insert=False, token="different")
    # WHEN: сервер выделяет настройки для одной сессии.
    current = session_settings(defaults, start)
    # THEN: меняются только разрешённые поля, исходный серверный объект сохраняется.
    assert asdict(current) == {**original, "recognition_pause": 5, "save_recordings": True}
    assert asdict(defaults) == original


def test_native_start_with_correct_unicode_token_is_accepted():
    # GIVEN: клиент и сервер используют один токен с кириллицей.
    settings = Settings(token="секрет")
    # WHEN: сервер читает стандартное сообщение клиента.
    start = read_start(json.dumps(make_start(settings, "session")), settings)
    # THEN: корректное сообщение принимается без изменения настроек.
    assert start == make_start(settings, "session")
    assert session_settings(settings, start) == settings


@pytest.mark.parametrize("mode", [False, "true", None, 1], ids=["window-mode", "string", "null", "number"])
def test_removed_recognition_mode_is_rejected_before_session_creation(mode):
    # GIVEN: внешний клиент запрашивает старый режим или неверный тип признака поддержки.
    defaults = Settings()
    start = make_start(defaults, "session") | {"recognize_on_pause": mode}
    # WHEN: сервер проверяет параметры сессии.
    with pytest.raises(ValueError, match="только распознавание целых блоков"):
        session_settings(defaults, start)
    # THEN: режим не заменяется молча другим алгоритмом.
    assert defaults == Settings()


@pytest.mark.parametrize(
    "event",
    [
        pytest.param({"type": "ready", "recognize_on_pause": True}, id="minimal-ready"),
        pytest.param({"type": "commit", "seq": 1, "delta": " Текст.", "decode_seconds": 0}, id="commit"),
        pytest.param(
            {"type": "recognizing", "audio_start": 0, "audio_end": 3.5, "capturing": True}, id="recognizing"
        ),
        pytest.param({"type": "segment_end", "session_id": ""}, id="segment-end"),
        pytest.param({"type": "session_end", "text": "", "recording_path": "/recordings/one"}, id="terminal"),
        pytest.param({"type": "cancelled", "text": "Подтверждено."}, id="cancelled"),
        pytest.param(
            {"type": "recording", "status": "error", "message": "Диск заполнен"}, id="recording-error"
        ),
        pytest.param({"type": "error", "message": "Занято"}, id="error-without-session-id"),
        pytest.param({"type": "ready", "recognition_pause": 0.5}, id="minimum-pause"),
        pytest.param({"type": "ready", "recognition_pause": 10}, id="maximum-pause"),
    ],
)
def test_server_event_preserves_v1_fields_and_extensions(event):
    # GIVEN: корректный ответ v1 с дополнительными данными расширения.
    payload = {**event, "extension": {"some": [1, True, None]}}
    # WHEN: проверяем сетевой JSON до передачи потребителю.
    result = read_server_event(json.dumps(payload, ensure_ascii=False))
    # THEN: значимый контракт и дополнительные поля сохранены без преобразований.
    assert type(result) is dict and result == payload


@pytest.mark.parametrize(
    "message,field",
    [
        pytest.param(b"{}", "JSON", id="binary-message"),
        pytest.param("{", "JSON", id="broken-json"),
        pytest.param("[]", "объект", id="not-object"),
        pytest.param('{"type":7}', "type", id="type-is-not-text"),
        pytest.param('{"type":"future"}', "неизвестное событие", id="unsupported-event"),
        pytest.param('{"type":"commit","seq":true,"delta":"Текст"}', "seq", id="bool-sequence"),
        pytest.param('{"type":"commit","seq":0,"delta":"Текст"}', "seq", id="zero-sequence"),
        pytest.param('{"type":"commit","seq":1}', "delta", id="missing-delta"),
        pytest.param('{"type":"commit","seq":1,"delta":""}', "delta", id="empty-delta"),
        pytest.param('{"type":"error"}', "message", id="missing-error-message"),
        pytest.param('{"type":"cancelled","text":null}', "text", id="null-cancelled-text"),
        pytest.param('{"type":"ready","session_id":1}', "session_id", id="invalid-session-id"),
        pytest.param(
            '{"type":"ready","recognize_on_pause":1}', "recognize_on_pause", id="non-bool-capability"
        ),
        pytest.param('{"type":"ready","recognition_pause":0.49}', "recognition_pause", id="pause-too-small"),
        pytest.param('{"type":"ready","recognition_pause":10.01}', "recognition_pause", id="pause-too-large"),
        pytest.param(
            '{"type":"session_end","text":"","audio_seconds":true}', "audio_seconds", id="bool-duration"
        ),
        pytest.param(
            '{"type":"session_end","text":"","audio_seconds":NaN}', "audio_seconds", id="nan-duration"
        ),
        pytest.param(
            '{"type":"session_end","text":"","elapsed_seconds":Infinity}',
            "elapsed_seconds",
            id="infinite-duration",
        ),
        pytest.param(
            '{"type":"commit","seq":1,"delta":"Текст","decode_seconds":-1}',
            "decode_seconds",
            id="negative-duration",
        ),
        pytest.param(
            '{"type":"recording","status":"saved","message":""}', "status", id="local-status-on-wire"
        ),
        pytest.param(
            '{"type":"recognizing","audio_start":0,"audio_end":1,"capturing":1}',
            "capturing",
            id="non-bool-capturing",
        ),
    ],
)
def test_invalid_server_event_is_rejected_with_visible_field_error(message, field):
    # GIVEN: сетевой ответ нарушает значимый контракт v1.
    # WHEN: проверяем сообщение до любых эффектов потребителя.
    with pytest.raises(ValueError, match=f"Некорректный ответ сервера:.*{field}"):
        read_server_event(message)
    # THEN: отказ содержит поле или вид нарушения, исходное сообщение не выдано потребителю.
