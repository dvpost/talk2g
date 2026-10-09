# Сторонние компоненты

Приложение написано самостоятельно; код OpenWhispr и других диктовщиков не включён.

- [GigaAM](https://github.com/salute-developers/GigaAM), GigaChat Team, MIT.
  ONNX INT8-конвертация [istupakov/gigaam-v3-onnx](https://huggingface.co/istupakov/gigaam-v3-onnx),
  revision `322c3b29492673eb7d0b434bfa9dfb8653e34d02`.
  Текст лицензии поставляется в каждом каталоге модели.
- [Silero VAD](https://github.com/snakers4/silero-vad), Silero Team, MIT.
  ONNX [istupakov/silero-vad-onnx](https://huggingface.co/istupakov/silero-vad-onnx),
  revision `b3e3ee3cce4c11ceb63b1a0b229d916069c1ddf6`.
- [onnx-asr](https://github.com/istupakov/onnx-asr), загрузка моделей и препроцессинг, MIT.
- ONNX Runtime, NumPy, SciPy, soundfile, sounddevice, websockets, python-xlib,
  dbus-next и huggingface-hub поставляются под своими лицензиями.
  Их версии закреплены в `uv.lock`.
- PySide6 / Qt имеют отдельные условия LGPL/GPL/commercial. При сборке PyInstaller
  используются отдельные динамические библиотеки Qt; сведения и тексты лицензий
  поставляются в `licenses` автономной папки. Код приложения не изменяет Qt.
- Локальная `libxcb-cursor0` взята из репозитория Ubuntu 24.04; её сведения
  доступны в `.tools/system-libs/usr/share/doc/libxcb-cursor0`.
- `tests/fixtures/example.wav` — официальный демонстрационный файл GigaAM,
  источник: [example.wav](https://cdn.chatwm.opensmodel.sberdevices.ru/GigaAM/example.wav).
  Используется только для проверок распознавания; не требуется при диктовке.
- Синтетические `dual-technical.wav`, `dual-repetitions.wav`, `dual-names_numbers.wav`
  и `benchmarks/voice-command.wav` созданы для тестов через
  [Piper](https://github.com/OHF-Voice/piper1-gpl) и
  [ru_RU-denis-medium](https://huggingface.co/rhasspy/piper-voices/tree/main/ru/ru_RU/denis/medium).
  Карточка голоса указывает CC0 для исходного датасета. Piper и веса голоса
  не входят в приложение. `dual-long.wav` — три повтора официального примера GigaAM.

При распространении сохраняйте тексты лицензий внутри `models` и `licenses`.
