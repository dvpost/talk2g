# Проверка проекта

Все команды выполняются из корня репозитория исходников. Для разработки
и тестов нужен клон проекта; автономная поставка не содержит тестовую среду.

## Установка и основные проверки

```bash
uv sync --frozen --extra desktop --extra dev
./check.sh
```

`check.sh` проверяет зависимости из `uv.lock`, запускает Ruff, проверку
форматирования, локальных ссылок документации и pytest. По умолчанию Qt
использует `offscreen`. Команды проверки одного сценария или подсистемы:

```bash
./check.sh tests/test_dictated_text.py
./check.sh tests/test_protocol.py tests/test_server.py tests/test_client.py
uv run python scripts/check_docs.py
```

[check_docs.py](../scripts/check_docs.py) проверяет существование файлов и
каталогов, указанных в inline-ссылках корневых Markdown-документов, `docs/` и `TODO/`.
Примеры в code blocks/inline code пропускаются. Внешние URL и якоря `#...`
не проверяются; инструмент не оценивает достоверность текста.

GitHub Actions выполняет статические проверки, проверку ссылок и offscreen
pytest на Linux и Windows, затем отдельно запускает native X11-тесты на Linux
в Xvfb. Конфигурация — [.github/workflows/check.yml](../.github/workflows/check.yml).
Настоящая модель и интерактивные Windows/Wayland-сценарии в CI не запускаются.

На Windows после установки dev-зависимостей обычные gates запускаются напрямую:

```powershell
uv sync --frozen --extra desktop --extra dev
$env:QT_QPA_PLATFORM = "offscreen"
uv run ruff check src tests benchmarks packaging scripts
uv run ruff format --check src tests benchmarks packaging scripts
uv run python scripts/check_docs.py
uv run pytest -q
```

Если окружение установлено через `setup-windows.ps1`, сначала задайте
`$env:UV_PROJECT_ENVIRONMENT = ".venv-win"`, чтобы проверять то же окружение,
которое использует `run-windows.cmd`. Если `uv` не доступен в PATH,
используйте `./.tools/uv.exe` вместо `uv` после выполнения установщика.

## Выбор проверок

Общие правила тестирования — §5 конвенции, источник закреплён в
[AGENTS.md](../AGENTS.md#правила-работы).
В проекте используются pytest, pytest-asyncio и pytest-qt; комментарии сценариев
пишутся по-русски. Qt по умолчанию работает без рабочего стола.

| Область изменения | Проверка |
| --- | --- |
| Документация | `uv run python scripts/check_docs.py` и сверка команд/контрактов |
| Текст, PCM, настройки | Соответствующие unit-тесты через `./check.sh tests/<file>.py` |
| WebSocket, SQLite, WAV, очередь захвата | Интеграционные тесты соответствующих компонентов через `./check.sh` |
| Ввод, хоткей, clipboard | Обычные gates и изолированные native-проверки ниже |
| Распознавание настоящей моделью | Проверки `real_model` после загрузки весов |
| Автономная поставка | Сборка на целевой ОС и проверка бинарника |

## Карта проверок и границы среды

| Контур | Тесты и наблюдаемая граница |
| --- | --- |
| Речь и текст | [test_audio.py](../tests/test_audio.py), [test_transcript.py](../tests/test_transcript.py), [test_dictated_text.py](../tests/test_dictated_text.py): сегментация, текстовые блоки и голосовая команда |
| Сессионный протокол | [test_protocol.py](../tests/test_protocol.py), [test_client.py](../tests/test_client.py), [test_server.py](../tests/test_server.py), [test_cli.py](../tests/test_cli.py): валидация JSON/PCM; интеграция клиента и сервера через реальный WebSocket |
| Расписание модели | [test_session.py](../tests/test_session.py): публичные feed/stop/run с управляемыми часами, без настоящей модели |
| Архив | [test_session_output.py](../tests/test_session_output.py), [test_server.py](../tests/test_server.py): настоящий WAV/метаданные, вход модели и закрытие до терминального события |
| Настройки и история | [test_settings_history.py](../tests/test_settings_history.py), [test_ui_pages.py](../tests/test_ui_pages.py): файлы профиля, SQLite и действия виджетов |
| Контроллер и ввод | [test_desktop_controls.py](../tests/test_desktop_controls.py), [test_insertion.py](../tests/test_insertion.py): UI и очередь с doubles системных границ; [test_desktop.py](../tests/test_desktop.py) — настоящий ввод в отдельные окна |
| Системные службы | [test_service.py](../tests/test_service.py), [test_model.py](../tests/test_model.py): локальный HTTP и отказ на внешней C++-границе ONNX; [test_portal.py](../tests/test_portal.py) — Wayland с doubles DBus |
| Архитектура | [test_architecture.py](../tests/test_architecture.py): явные импорты, транзитивные зависимости и циклы; динамический importlib и внешние пакеты не анализируются |

Offscreen и doubles не заменяют native Windows/Wayland или проверку модели.
Для системного ввода используются свои тестовые окна и профили;
[реестр исключений](exceptional_execution_paths.md) связывает EXC-ID с их проверками.

Покрытие можно измерить без добавления зависимости в проект:

```bash
env TALK2G_REAL_MODEL_TESTS=1 QT_QPA_PLATFORM=offscreen uv run --frozen --extra desktop --extra dev --with coverage python -m coverage run --branch --data-file=/tmp/talk2g.coverage -m pytest -q
uv run --frozen --extra desktop --extra dev --with coverage python -m coverage report --data-file=/tmp/talk2g.coverage --include='src/talk2g/*'
```

## Настоящая модель и системный ввод

Подготовьте веса GigaAM:

```bash
./run-linux.sh download
env TALK2G_REAL_MODEL_TESTS=1 ./check.sh
```

`tests/test_real_model.py` передаёт `tests/fixtures/example.wav` через настоящий
сервер и проверяет целые блоки после паузы и Stop, порядок commit и совпадение итогового текста
с полученными фрагментами, а также сохранение точного PCM.

Для системного ввода и глобальной горячей клавиши в отдельном X11-сеансе:

```bash
env TALK2G_REAL_MODEL_TESTS=1 benchmarks/check_x11.sh
```

Только native-тесты, как в Linux CI, без весов модели:

```bash
benchmarks/check_x11.sh -m desktop
```

Скрипт использует Xvfb, XFWM4 и отдельную D-Bus-сессию. Требуются `xvfb-run`,
`xfwm4`, `xprop`, `setxkbmap`, `dbus-run-session`, `xdotool` и
`xfce4-terminal`; при наличии используются инструменты из `.tools`.
Скрипт задаёт отдельное X11-окружение, в том числе при запуске из Wayland.
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
завершения. На время проверки системный источник записи по умолчанию меняется
на тестовый монитор PulseAudio. Запускайте проверку отдельно от других программ,
записывающих звук, чтобы не вмешаться в их запись.
Графическим проверкам на машине без системной `libxcb-cursor0`
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
Документация находится в `dist/talk2g/docs`, README, AGENTS и LICENSE —
в корне поставки. Переносите папку целиком; Python и uv для неё не нужны,
системные требования Linux к PortAudio/xdotool остаются. Qt-библиотеки
поставляются отдельными файлами; тексты лицензий находятся в `licenses`.

`benchmarks/benchmark_models.py` сравнивает CTC и RNNT на одной записи.
Скрипты проверок сохраняют тексты, события и задержки в JSON; параметр `--output`
задаёт файл результата, если поддерживается конкретным скриптом.
Сгенерированные отчёты не входят в Git. Задержка и качество зависят от оборудования,
записи и настроек; результаты нужно получать на проверяемой версии приложения.
