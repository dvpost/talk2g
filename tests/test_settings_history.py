import pytest

from giga_dictation.config import Settings
from giga_dictation.history import History


def test_settings_are_saved_and_loaded_without_changing_other_folders(tmp_path):
    settings = Settings(
        microphone="Микрофон",
        auto_insert=False,
        show_overlay=False,
        load_on_demand=True,
        stop_on_phrase=True,
        dual_window=True,
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
        {"load_on_demand": "true"},
        {"autostart": "true"},
        {"stop_on_phrase": "true"},
        {"dual_window": "true"},
        {"fast_window": 0},
        {"quality_window": 25},
        {"quality_window": 5, "fast_window": 5},
        {"quality_interval": float("nan")},
        {"quality_holdback": -1},
    ],
)
def test_invalid_settings_are_rejected(values):
    with pytest.raises(ValueError):
        Settings(**values).validate()


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
