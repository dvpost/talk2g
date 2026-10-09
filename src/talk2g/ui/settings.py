"""Settings form: edit values and emit user intent without controlling capture."""

from dataclasses import replace
from urllib.parse import urlsplit

from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QWidget,
)

from ..config import Settings
from ..input import is_wayland
from ..recording import recordings_directory


class SettingsPage(QScrollArea):
    feature_changed = Signal(str, bool)
    position_changed = Signal()
    save_requested = Signal()
    wayland_requested = Signal()
    open_recordings_requested = Signal()

    def __init__(self, settings: Settings):
        super().__init__()
        self.microphone_error = ""
        widget = QWidget()
        form = QFormLayout(widget)
        self.url = QLineEdit(settings.server_url)
        form.addRow("Сервер", self.url)
        self.token = QLineEdit(settings.token)
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Токен удалённого сервера", self.token)
        self.microphone = QComboBox()
        self.microphone.addItem("Системный микрофон по умолчанию", "")
        try:
            import sounddevice as sd

            names = list(dict.fromkeys(d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0))
            for name in names:
                self.microphone.addItem(name, name)
        except Exception as error:
            self.microphone_error = f"Микрофон: {error}"
        if settings.microphone and self.microphone.findData(settings.microphone) < 0:
            self.microphone.addItem(settings.microphone + " (недоступен)", settings.microphone)
        index = self.microphone.findData(settings.microphone)
        if index >= 0:
            self.microphone.setCurrentIndex(index)
        form.addRow("Микрофон", self.microphone)
        self.hotkey_field = QLineEdit(settings.hotkey)
        form.addRow("Хоткей (Windows/X11)", self.hotkey_field)
        self.automatic = QCheckBox("Постепенно вводить текст в активное приложение")
        self.automatic.setChecked(settings.auto_insert)
        form.addRow(self.automatic)
        self.paste_method = QComboBox()
        for label, value in (
            ("Автоматически: терминал / обычное поле", "auto"),
            ("Ctrl+V", "ctrl_v"),
            ("Ctrl+Shift+V", "ctrl_shift_v"),
            ("Shift+Insert", "shift_insert"),
        ):
            self.paste_method.addItem(label, value)
        self.paste_method.setCurrentIndex(self.paste_method.findData(settings.paste_mode))
        form.addRow("Вставка в Linux", self.paste_method)
        self.copy_final = QCheckBox("После завершения копировать весь текст в буфер обмена")
        self.copy_final.setChecked(settings.copy_on_stop)
        form.addRow(self.copy_final)
        self.overlay_option = QCheckBox("Показывать синее окно с распознанным текстом")
        self.overlay_option.setChecked(settings.show_overlay)
        form.addRow(self.overlay_option)
        self.overlay_position_field = QComboBox()
        for label, value in (
            ("Справа сверху", "top_right"),
            ("Справа посередине", "middle_right"),
            ("Справа снизу", "bottom_right"),
            ("Слева сверху", "top_left"),
            ("Слева посередине", "middle_left"),
            ("Слева снизу", "bottom_left"),
        ):
            self.overlay_position_field.addItem(label, value)
        self.overlay_position_field.setCurrentIndex(
            self.overlay_position_field.findData(settings.overlay_position)
        )
        self.overlay_position_field.setToolTip("Применяется сразу и сохраняется между запусками.")
        self.overlay_position_field.currentIndexChanged.connect(self.position_changed.emit)
        form.addRow("Положение окна диктовки", self.overlay_position_field)
        self.demand_option = QCheckBox("Загружать модель только при диктовке")
        self.demand_option.setChecked(settings.load_on_demand)
        form.addRow(self.demand_option)
        model_note = QLabel(
            "С галочкой: запись начинается сразу, модель загружается параллельно "
            "и выгружается после остановки.\n"
            "Без галочки: модель постоянно в памяти, распознавание начинается быстрее."
        )
        model_note.setWordWrap(True)
        form.addRow(model_note)
        self.autostart_option = QCheckBox("Запускать при входе в систему")
        self.autostart_option.setChecked(settings.autostart)
        form.addRow(self.autostart_option)
        self.voice_stop_option = QCheckBox("Останавливать по фразе «конец связи»")
        self.voice_stop_option.setChecked(settings.stop_on_phrase)
        self.voice_stop_option.setToolTip(
            "Произнесите команду и сделайте короткую паузу. Команда завершает диктовку и не попадает в текст."
        )
        form.addRow(self.voice_stop_option)
        self.idle_stop_option = QCheckBox("Автозавершение без речи")
        self.idle_stop_option.setChecked(settings.stop_on_idle)
        form.addRow(self.idle_stop_option)
        self.save_recordings_option = QCheckBox("Сохранять аудиозаписи для разбора ошибок")
        self.save_recordings_option.setChecked(settings.save_recordings)
        self.save_recordings_option.setToolTip(
            "Сохраняет полученный сервером WAV и журнал распознавания. "
            "Изменение применяется к следующей диктовке. По умолчанию выключено."
        )
        form.addRow(self.save_recordings_option)
        self.open_recordings_button = QPushButton("Открыть папку записей")
        self.open_recordings_button.clicked.connect(self.open_recordings_requested.emit)
        form.addRow(self.open_recordings_button)
        for checkbox, name in (
            (self.automatic, "auto_insert"),
            (self.copy_final, "copy_on_stop"),
            (self.overlay_option, "show_overlay"),
            (self.demand_option, "load_on_demand"),
            (self.autostart_option, "autostart"),
            (self.voice_stop_option, "stop_on_phrase"),
            (self.idle_stop_option, "stop_on_idle"),
            (self.save_recordings_option, "save_recordings"),
        ):
            checkbox.toggled.connect(lambda checked, name=name: self.feature_changed.emit(name, checked))
        self.idle_timeout_field = QSpinBox()
        self.idle_timeout_field.setRange(1, 7200)
        self.idle_timeout_field.setSuffix(" с")
        self.idle_timeout_field.setValue(settings.idle_timeout)
        self.idle_timeout_field.setEnabled(settings.stop_on_idle)
        self.idle_timeout_field.setToolTip(
            "Диктовка завершается после паузы без речи. Новая речь сбрасывает отсчёт. "
            "По умолчанию — 45 секунд."
        )
        form.addRow("Тайм-аут без речи", self.idle_timeout_field)
        self.recognition_pause_field = QDoubleSpinBox()
        self.recognition_pause_field.setRange(0.5, 10)
        self.recognition_pause_field.setSingleStep(0.5)
        self.recognition_pause_field.setSuffix(" с")
        self.recognition_pause_field.setValue(settings.recognition_pause)
        self.recognition_pause_field.setToolTip(
            "Жёлтая часть полосы отсчитывает паузу до распознавания целого блока. По умолчанию — 3 секунды."
        )
        form.addRow("Пауза до распознавания", self.recognition_pause_field)
        save = QPushButton("Сохранить настройки")
        save.clicked.connect(self.save_requested.emit)
        form.addRow(save)
        if is_wayland():
            allow = QPushButton("Разрешить ввод и хоткей Wayland")
            allow.clicked.connect(self.wayland_requested.emit)
            form.addRow(allow)
            note = QLabel("Wayland вводит текст в текущее поле. Во время диктовки оставляйте фокус в нём.")
            note.setWordWrap(True)
            form.addRow(note)
        note = QLabel(
            "Модель: GigaAM v3 E2E · CPU INT8. После загрузки работает офлайн.\n"
            "Переключатели выше также доступны по правой кнопке на значке «t» в трее."
        )
        note.setWordWrap(True)
        form.addRow(note)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidget(widget)

    def values(self, current: Settings) -> Settings:
        return replace(
            current,
            server_url=self.url.text().strip(),
            token=self.token.text().strip(),
            microphone=self.microphone.currentData(),
            hotkey=self.hotkey_field.text().strip(),
            auto_insert=self.automatic.isChecked(),
            paste_mode=self.paste_method.currentData(),
            copy_on_stop=self.copy_final.isChecked(),
            show_overlay=self.overlay_option.isChecked(),
            overlay_position=self.overlay_position_field.currentData(),
            load_on_demand=self.demand_option.isChecked(),
            autostart=self.autostart_option.isChecked(),
            stop_on_phrase=self.voice_stop_option.isChecked(),
            stop_on_idle=self.idle_stop_option.isChecked(),
            idle_timeout=self.idle_timeout_field.value(),
            save_recordings=self.save_recordings_option.isChecked(),
            recognition_pause=self.recognition_pause_field.value(),
        )

    def sync(self, settings: Settings):
        for name, checkbox in (
            ("auto_insert", self.automatic),
            ("copy_on_stop", self.copy_final),
            ("show_overlay", self.overlay_option),
            ("load_on_demand", self.demand_option),
            ("autostart", self.autostart_option),
            ("stop_on_phrase", self.voice_stop_option),
            ("stop_on_idle", self.idle_stop_option),
            ("save_recordings", self.save_recordings_option),
        ):
            with QSignalBlocker(checkbox):
                checkbox.setChecked(getattr(settings, name))
        self.idle_timeout_field.setEnabled(settings.stop_on_idle)
        with QSignalBlocker(self.overlay_position_field):
            self.overlay_position_field.setCurrentIndex(
                self.overlay_position_field.findData(settings.overlay_position)
            )
        local = urlsplit(settings.server_url).hostname in ("127.0.0.1", "localhost", "::1")
        self.open_recordings_button.setEnabled(local)
        self.open_recordings_button.setToolTip(
            str(recordings_directory()) if local else "Записи сохраняются на компьютере сервера распознавания"
        )
