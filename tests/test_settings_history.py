import json

import pytest

from talk2g.config import Settings
from talk2g.history import History


def test_settings_are_saved_and_loaded_without_changing_other_folders(tmp_path):
    settings = Settings(
        microphone="Микрофон",
        auto_insert=False,
        show_overlay=False,
        overlay_position="middle_left",
        load_on_demand=True,
        stop_on_phrase=True,
        stop_on_idle=False,
        idle_timeout=90,
        save_recordings=True,
        recognize_on_pause=False,
        recognition_pause=5,
        interval=0.9,
    )
    settings.save(tmp_path)
    assert Settings.load(tmp_path) == settings


@pytest.mark.parametrize(
    "values",
    [
        {"threads": True},
        {"interval": float("inf")},
        {"silence": -1},
        {"model": "untrusted-model"},
        {"server_url": "ftp://example"},
        {"paste_mode": "unknown"},
        {"copy_on_stop": "true"},
        {"show_overlay": "true"},
        {"overlay_position": "center"},
        {"overlay_position": None},
        {"load_on_demand": "true"},
        {"autostart": "true"},
        {"stop_on_phrase": "true"},
        {"stop_on_idle": "true"},
        {"save_recordings": "true"},
        {"recognize_on_pause": "true"},
        {"recognition_pause": True},
        {"recognition_pause": 0},
        {"recognition_pause": 11},
        {"recognition_pause": float("nan")},
        {"idle_timeout": True},
        {"idle_timeout": 0},
        {"idle_timeout": -1},
        {"idle_timeout": 7201},
        {"idle_timeout": 1.5},
        {"idle_timeout": "45"},
        {"idle_timeout": float("inf")},
    ],
)
def test_invalid_settings_are_rejected(values):
    with pytest.raises(ValueError):
        Settings(**values).validate()


def test_unknown_settings_are_ignored_and_not_saved(tmp_path):
    path = tmp_path / ".data" / "settings.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "microphone": "Микрофон",
                "auto_insert": False,
                "load_on_demand": True,
                "stop_on_phrase": True,
                "interval": 0.9,
                "unknown_option": True,
                "unknown_number": 5,
            }
        ),
        encoding="utf-8",
    )
    settings = Settings.load(tmp_path)
    assert settings.idle_timeout == 45
    assert settings.overlay_position == "top_right"
    assert settings.stop_on_idle
    assert settings.recognize_on_pause and settings.recognition_pause == 3
    assert not settings.save_recordings
    assert settings == Settings(
        microphone="Микрофон",
        auto_insert=False,
        load_on_demand=True,
        stop_on_phrase=True,
        interval=0.9,
    )
    settings.save(tmp_path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "unknown_option" not in saved and "unknown_number" not in saved
    assert Settings.load(tmp_path) == settings


def test_history_preserves_text_and_bounds_retention(tmp_path):
    history = History(tmp_path)
    for i in range(105):
        history.add(f"Русский текст {i}")
    entries = history.recent()
    assert len(entries) == 100
    assert entries[0][1] == "Русский текст 104"
    assert entries[-1][1] == "Русский текст 5"
    history.clear()
    assert history.recent() == []
    history.close()
