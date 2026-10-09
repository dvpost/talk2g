import configparser
import shutil
import subprocess
import sys
import time

import pytest

from talk2g.autostart import RUN_KEY, VALUE_NAME, Autostart


def test_linux_enable_disable_is_idempotent_and_preserves_other_autostart_entries(tmp_path):
    manager = Autostart(home=tmp_path, config_dir=tmp_path / "xdg", platform="linux")
    manager.set_enabled(False)
    assert not manager.is_enabled()
    manager.entry.parent.mkdir(parents=True)
    other = manager.entry.with_name("another-app.desktop")
    other.write_text("Other application")
    manager.set_enabled(True)
    first = manager.entry.read_bytes()
    manager.set_enabled(True)
    assert manager.entry.read_bytes() == first and manager.is_enabled()
    if shutil.which("desktop-file-validate"):
        subprocess.run(["desktop-file-validate", str(manager.entry)], check=True)
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(manager.entry)
    assert parser["Desktop Entry"]["Terminal"] == "false"
    assert "--background" in parser["Desktop Entry"]["Exec"]
    manager.set_enabled(False)
    manager.set_enabled(False)
    assert not manager.is_enabled() and not manager.entry.exists()
    assert other.read_text() == "Other application"


@pytest.mark.parametrize("flag", ["Hidden=true", "X-GNOME-Autostart-enabled=false"])
def test_linux_checkbox_detects_external_disabling(tmp_path, flag):
    manager = Autostart(home=tmp_path, config_dir=tmp_path, platform="linux")
    manager.set_enabled(True)
    content = manager.entry.read_text().replace("X-GNOME-Autostart-enabled=true\n", "")
    manager.entry.write_text(content + flag + "\n")
    assert not manager.is_enabled()


@pytest.mark.parametrize(
    ("content", "error"),
    [
        pytest.param("invalid desktop entry", configparser.Error, id="invalid-format"),
        pytest.param("[Other]\nType=Application\n", KeyError, id="missing-section"),
        pytest.param(
            "[Desktop Entry]\nType=Application\nExec=talk2g\nHidden=perhaps\n",
            ValueError,
            id="invalid-boolean",
        ),
    ],
)
def test_invalid_autostart_entry_reports_error_instead_of_disabled_state(tmp_path, content, error):
    # GIVEN: существующий, но некорректный файл автозапуска.
    manager = Autostart(home=tmp_path, config_dir=tmp_path, platform="linux")
    manager.entry.parent.mkdir(parents=True)
    manager.entry.write_text(content)
    # WHEN: приложение читает состояние автозапуска.
    with pytest.raises(error):
        manager.is_enabled()
    # THEN: ошибка не маскируется выключенной галочкой, исходный файл сохранён.
    assert manager.entry.read_text() == content


@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("gio"), reason="Linux desktop Exec parsing")
def test_real_desktop_launcher_preserves_spaces_quotes_backslashes_dollars_and_percent(tmp_path):
    # Use the actual desktop launcher as the parser. Shell quoting would give a
    # misleading pass because Exec has two different escaping stages.
    script = tmp_path / "capture.py"
    script.write_text("import pathlib, sys\npathlib.Path(sys.argv[1]).write_text(sys.argv[2])\n")
    captured = tmp_path / "result.txt"
    profile = tmp_path / 'Русская папка $HOME `tick` "quote" back\\slash %f'
    profile.mkdir()
    manager = Autostart(home=profile, config_dir=tmp_path / "xdg", platform="linux")
    manager.command = lambda: [sys.executable, str(script), str(captured), str(profile)]
    manager.set_enabled(True)
    subprocess.run(["desktop-file-validate", str(manager.entry)], check=True)
    subprocess.run(["gio", "launch", str(manager.entry)], check=True)
    deadline = time.monotonic() + 3
    while not captured.exists() and time.monotonic() < deadline:
        time.sleep(0.03)
    assert captured.read_text() == str(profile)


def test_xdg_override_and_frozen_command_keep_correct_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "custom-config"))
    executable = str(tmp_path / "talk2g")
    manager = Autostart(home=tmp_path / "profile", executable=executable, platform="linux", frozen=True)
    assert manager.entry == tmp_path / "custom-config/autostart/talk2g.desktop"
    assert manager.command() == [
        executable,
        "app",
        "--background",
        "--home",
        str(tmp_path / "profile"),
    ]


class RegistryKey:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class Registry:
    HKEY_CURRENT_USER = "current-user"
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values = {}

    def OpenKey(self, root, path, *args):
        assert root == self.HKEY_CURRENT_USER
        if path not in self.values:
            raise FileNotFoundError(path)
        return RegistryKey(path)

    def CreateKeyEx(self, root, path, *args):
        assert root == self.HKEY_CURRENT_USER
        self.values.setdefault(path, {})
        return RegistryKey(path)

    def QueryValueEx(self, key, name):
        if name not in self.values[key.path]:
            raise FileNotFoundError(name)
        return self.values[key.path][name]

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[key.path][name] = value, kind

    def DeleteValue(self, key, name):
        if name not in self.values[key.path]:
            raise FileNotFoundError(name)
        del self.values[key.path][name]


def test_windows_run_key_uses_windowless_python_and_preserves_other_entries(tmp_path_factory):
    # HKCU Run limits the complete command to 260 characters. The default
    # per-test temporary directory name can exceed that budget on Windows.
    tmp_path = tmp_path_factory.mktemp("run")
    registry = Registry()
    registry.values[RUN_KEY] = {"OtherApp": ("unchanged", registry.REG_SZ)}
    executable = tmp_path / "Python with spaces" / "python.exe"
    executable.parent.mkdir()
    executable.with_name("pythonw.exe").touch()
    manager = Autostart(
        home=tmp_path / "profile",
        executable=str(executable),
        platform="win32",
        registry=registry,
        frozen=False,
    )
    assert not manager.is_enabled()
    manager.set_enabled(True)
    assert manager.is_enabled()
    command = registry.values[RUN_KEY][VALUE_NAME][0]
    assert 'pythonw.exe" -m talk2g app --background --home' in command
    manager.set_enabled(False)
    manager.set_enabled(False)
    assert not manager.is_enabled()
    assert registry.values[RUN_KEY] == {"OtherApp": ("unchanged", registry.REG_SZ)}


def test_windows_rejects_overlong_run_command_before_modifying_registry(tmp_path):
    registry = Registry()
    manager = Autostart(
        home=tmp_path / ("a" * 230),
        platform="win32",
        registry=registry,
        executable="C:/talk2g.exe",
        frozen=True,
    )
    with pytest.raises(ValueError, match="слишком длинный"):
        manager.set_enabled(True)
    assert not registry.values
