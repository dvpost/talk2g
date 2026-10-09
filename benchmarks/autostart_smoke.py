"""Exercise an actual Linux login entry, without rebooting or altering user startup files."""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PySide6.QtWidgets import QApplication

from talk2g.autostart import Autostart
from talk2g.config import Settings
from talk2g.desktop import send_control
from talk2g.service import health


def wait_for(condition, message, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError(message() if callable(message) else message)


def visible(pid):
    query = subprocess.run(
        ["xdotool", "search", "--onlyvisible", "--all", "--pid", str(pid), "--name", "^talk2g$"],
        capture_output=True,
        text=True,
    )
    return query.returncode == 0


def alive(pid):
    return pid is not None and Path(f"/proc/{pid}").exists()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", help="Frozen executable; omit to test the source virtualenv")
    parser.add_argument("--output", default="benchmarks/autostart-source-results.json")
    parser.add_argument(
        "--installed", action="store_true", help="Verify the user's existing entry; preserve it"
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    application = QApplication([])
    if send_control("ping"):
        raise RuntimeError("Завершите обычный talk2g перед проверкой его автозапуска")
    executable = str(Path(args.executable).resolve()) if args.executable else sys.executable
    pid = None
    with tempfile.TemporaryDirectory(prefix="autostart-", dir=root / ".tools") as temporary:
        profile = root if args.installed else Path(temporary)
        settings = Settings.load(profile) if args.installed else Settings(load_on_demand=True, autostart=True)
        if not args.installed:
            settings.save(profile)
        manager = Autostart(
            home=profile,
            config_dir=None if args.installed else profile / "xdg",
            executable=executable,
            frozen=bool(args.executable),
        )
        environment = dict(
            os.environ,
            XDG_CONFIG_HOME=str(manager.config_dir),
            HF_HUB_OFFLINE="1",
            TALK2G_HOME=str(profile / "wrong-profile"),
        )
        # Login sessions do not inherit our development shell's local Qt setup.
        environment.pop("LD_LIBRARY_PATH", None)
        log_path = profile / ".data/app.log"
        launcher_log = Path(temporary) / "startup-launch.log"
        previous_log_size = len(log_path.read_text()) if log_path.exists() else 0
        result = {
            "executable": executable,
            "mechanism": "GIO launch of XDG .desktop",
            "fresh_login_env": True,
        }

        def launch():
            with launcher_log.open("a") as stream:
                subprocess.run(
                    ["gio", "launch", str(manager.entry)],
                    env=environment,
                    stdout=stream,
                    stderr=stream,
                    check=True,
                    timeout=5,
                )

        def log():
            return log_path.read_text()[previous_log_size:] if log_path.exists() else ""

        try:
            if not args.installed:
                manager.set_enabled(True)
            assert manager.is_enabled()
            subprocess.run(["desktop-file-validate", str(manager.entry)], check=True)
            began = time.monotonic()
            launch()
            wait_for(lambda: "Интерфейс готов:" in log(), launcher_log.read_text)
            pid = int(re.search(r"Интерфейс готов: pid=(\d+)", log())[1])
            result["startup_seconds"] = time.monotonic() - began
            assert "фон=True" in log() and "недоступен" not in log(), log()
            assert not visible(pid), "Автозапуск открыл главное окно"
            assert send_control("ping")
            result["started_in_tray"] = True
            assert not (profile / "wrong-profile/.data").exists()
            result["correct_profile"] = True
            try:
                health(settings.server_url)
            except OSError:
                result["model_absent_until_activation"] = True
            else:
                raise AssertionError("Модель загружена вопреки настройке по требованию")
            launch()  # a second login/manual background launch must leave one instance hidden
            time.sleep(1.5)
            assert alive(pid) and not visible(pid)
            assert log().count("Интерфейс готов:") == 1
            result["duplicate_launch_does_not_show_or_duplicate_app"] = True
            assert send_control("show")
            wait_for(lambda: visible(pid), "Не открылось окно по запросу")
            result["window_opens_on_request"] = True
            assert send_control("quit")
            wait_for(lambda: not alive(pid), "Приложение не завершилось")
            result["clean_exit"] = True
            if not args.installed:
                manager.set_enabled(False)
                assert not manager.is_enabled() and not manager.entry.exists()
                result["entry_removed_on_disable"] = True
            else:
                assert manager.is_enabled()
                result["installed_entry_preserved"] = True
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False))
        finally:
            (root / "benchmarks/autostart-last.log").write_text(
                launcher_log.read_text() if launcher_log.exists() else "", encoding="utf-8"
            )
            if alive(pid):
                send_control("quit")
                try:
                    wait_for(lambda: not alive(pid), "Ожидание завершения", timeout=5)
                except AssertionError:
                    os.kill(pid, signal.SIGTERM)
            if not args.installed:
                manager.set_enabled(False)
    del application


if __name__ == "__main__":
    main()
