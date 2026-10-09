# Проверка проекта

## Установка и основные проверки

```bash
uv sync --frozen --extra desktop --extra dev
./check.sh
```

`check.sh` проверяет зависимости из `uv.lock`, запускает Ruff, проверку
форматирования и pytest. По умолчанию Qt использует `offscreen`.
GitHub Actions выполняет те же проверки на Linux и Windows;
конфигурация — `.github/workflows/check.yml`.

Тесты покрывают сегментацию аудио и VAD, перекрытия и хвост записи,
подтверждение слов по тексту и времени, повторённые слова, последовательность
событий WebSocket, Stop и Cancel, ошибки распознавания, авторизацию,
одну активную сессию, загрузку модели по требованию, настройки,
горячую клавишу, буфер обмена, автозапуск и историю диктовок.

## Настоящая модель и системный ввод

Подготовьте веса GigaAM:

```bash
./run-linux.sh download
env TALK2G_REAL_MODEL_TESTS=1 ./check.sh
```

`tests/test_real_model.py` передаёт `tests/fixtures/example.wav` через настоящий
сервер и проверяет появление текста во время передачи аудио, порядок commit
и совпадение итогового текста с полученными фрагментами.

Для системного ввода и глобальной горячей клавиши в отдельном X11-сеансе:

```bash
env TALK2G_REAL_MODEL_TESTS=1 benchmarks/check_x11.sh
```

Скрипт использует Xvfb, XFWM4 и отдельную D-Bus-сессию. Требуются `xvfb-run`,
`xfwm4`, `xprop` и `setxkbmap`; при наличии используются инструменты из `.tools`.
Тесты ввода работают со своими окнами. Проверки Windows/Wayland на реальном
рабочем столе требуют соответствующей системы; offscreen-тесты их не заменяют.

## Сквозные проверки

`benchmarks/e2e_desktop.py` и `benchmarks/long_stream.py` используют работающий
сервер. Запустите `./run-linux.sh server` в отдельном терминале, затем:

```bash
uv run python benchmarks/e2e_desktop.py
uv run python benchmarks/e2e_desktop.py --microphone-pulse
uv run python benchmarks/long_stream.py
```

На Linux `--microphone-pulse` подаёт запись через виртуальный источник PulseAudio
в настоящий PortAudio InputStream и восстанавливает исходный источник после
завершения. Графическим проверкам на машине без системной `libxcb-cursor0`
нужен `LD_LIBRARY_PATH="$PWD/.tools/system-libs/usr/lib/x86_64-linux-gnu"`.

Для проверки запуска приложения, хоткея, микрофона и вставки в терминал:

```bash
uv run python benchmarks/e2e_packaged.py --on-demand --terminal --sessions 2
uv run python benchmarks/e2e_packaged.py --on-demand --terminal --voice-stop --audio benchmarks/voice-stop-input.wav
uv run python benchmarks/autostart_smoke.py
```

Эти проверки создают временный профиль и запускают своё приложение. Перед ними
завершите обычное приложение, чтобы освободить глобальный хоткей.
`autostart_smoke.py` проверяет запись автозапуска, запуск через системный launcher,
фоновый режим и управление одним экземпляром приложения.

`benchmarks/voice-stop-input.wav` содержит речь, команду «Конец связи.», тишину
и продолжение речи. Временная разметка находится в `voice-stop-input.json`;
`make_voice_stop_audio.py` собирает эту запись из аудиофайлов.

## Сборка и измерения

Сборка выполняется на целевой ОС:

```bash
uv run python packaging/build.py
uv run python benchmarks/e2e_packaged.py --executable dist/talk2g/talk2g
uv run python benchmarks/autostart_smoke.py --executable dist/talk2g/talk2g
```

На Windows исполняемый файл — `dist/talk2g/talk2g.exe`.
Сборка содержит GigaAM CTC, Silero VAD и зависимости приложения.

`benchmarks/benchmark_models.py` сравнивает CTC и RNNT на одной записи.
Скрипты проверок сохраняют тексты, события и задержки в JSON; параметр `--output`
задаёт файл результата, если поддерживается конкретным скриптом.
Сгенерированные отчёты не входят в Git. Задержка и качество зависят от оборудования,
записи и настроек; результаты нужно получать на проверяемой версии приложения.
