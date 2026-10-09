"""Real GigaAM -> WebSocket -> Qt client -> native X11/Windows text insertion.

Only writes to the test's own target window. Saves result and screenshot for inspection.
"""

import argparse
import json
import subprocess
import time
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QPlainTextEdit

from talk2g.config import Settings
from talk2g.desktop import MainWindow


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", default="tests/fixtures/example.wav")
    parser.add_argument("--output", default="benchmarks/desktop-results.json")
    parser.add_argument(
        "--microphone-pulse",
        action="store_true",
        help="Linux: test real PortAudio input using a temporary PulseAudio monitor",
    )
    parser.add_argument("--hotkey", action="store_true", help="X11: activate actual global Ctrl+Shift+A")
    args = parser.parse_args()
    application = QApplication([])
    application.setQuitOnLastWindowClosed(False)
    window = MainWindow(Settings(auto_insert=True))
    chord = "+".join(part.strip("<>") for part in window.settings.hotkey.split("+"))
    target = QPlainTextEdit()
    target.setWindowTitle("talk2g · тестовое поле ввода")
    target.resize(850, 350)
    target.show()
    target.activateWindow()
    target.setFocus()
    began = time.monotonic()
    started = False
    first_insert = None
    error = ""
    player = None
    module = None
    original_source = ""
    stop_sent = False
    if args.microphone_pulse:
        original_source = subprocess.check_output(["pactl", "get-default-source"], text=True).strip()
        module = subprocess.check_output(
            ["pactl", "load-module", "module-null-sink", "sink_name=talk2g_test"], text=True
        ).strip()
        subprocess.run(["pactl", "set-default-source", "talk2g_test.monitor"], check=True)

    def start():
        nonlocal player
        if args.microphone_pulse:
            if args.hotkey:
                subprocess.run(["xdotool", "key", chord], check=True)
            else:
                window.start_recording()
            # Give PortAudio time to open before playback starts.
            QTimer.singleShot(800, play)
        else:
            window.start_recording(audio_file=str(Path(args.audio).resolve()))

    def play():
        nonlocal player
        player = subprocess.Popen(["paplay", "--device=talk2g_test", args.audio])

    def poll():
        nonlocal started, first_insert, error, began, stop_sent
        try:
            if not started:
                if not window.ready:
                    if time.monotonic() - began > 120:
                        raise RuntimeError(window.status.text())
                    return
                window.hide()
                target.raise_()
                target.activateWindow()
                target.setFocus()
                started = True
                began = time.monotonic()
                QTimer.singleShot(300, start)
                return
            if (
                player is not None
                and player.poll() is not None
                and window.thread is not None
                and not stop_sent
            ):
                stop_sent = True
                if args.hotkey:
                    subprocess.run(["xdotool", "key", chord], check=True)
                else:
                    window.thread.stop()
            if target.toPlainText() and first_insert is None:
                first_insert = time.monotonic() - began
                target.grab().save("benchmarks/desktop-running.png")
            if window.thread is None and window.delivery.text and not window.inserter.busy:
                actual = target.toPlainText()
                expected = window.delivery.text
                result = {
                    "inserted_text": actual,
                    "recognized_text": expected,
                    "matches": actual == expected,
                    "first_insert_seconds": first_insert,
                    "elapsed_seconds": time.monotonic() - began,
                    "status": window.status.text(),
                    "activation": "global hotkey" if args.hotkey else "API",
                    "audio_source": "PortAudio/PulseAudio monitor"
                    if args.microphone_pulse
                    else "file replay",
                }
                Path(args.output).write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                if actual != expected:
                    raise AssertionError(f"Вставленный текст отличается: {actual!r} != {expected!r}")
                if first_insert is None or first_insert > 5:
                    raise AssertionError("Текст не появился во время диктовки")
                print(json.dumps(result, ensure_ascii=False))
                timer.stop()
                window.quit()
            if time.monotonic() - began > 60:
                raise RuntimeError("Диктовка не завершилась: " + window.status.text())
        except Exception as exception:
            error = str(exception)
            print(error)
            timer.stop()
            window.quit()

    timer = QTimer()
    timer.timeout.connect(poll)
    timer.start(100)
    try:
        application.exec()
    finally:
        if player and player.poll() is None:
            player.terminate()
            player.wait(timeout=3)
        if module is not None:
            subprocess.run(["pactl", "set-default-source", original_source], check=True)
            subprocess.run(["pactl", "unload-module", module], check=True)
    if error:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
