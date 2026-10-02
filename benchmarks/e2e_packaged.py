"""Test the actual launcher/executable with a fresh profile and owned ASR server.

Linux X11: global hotkey -> PortAudio virtual microphone -> live insertion -> history -> graceful quit.
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QPlainTextEdit
from Xlib import display

from giga_dictation.config import Settings
from giga_dictation.history import History
from giga_dictation.service import health


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", default="./run-linux.sh")
    parser.add_argument("--output", default="benchmarks/launcher-results.json")
    parser.add_argument("--terminal", action="store_true", help="Use real XFCE Terminal as the target")
    parser.add_argument("--sessions", type=int, default=1)
    parser.add_argument("--on-demand", action="store_true")
    parser.add_argument("--stop-after", type=float, help="Stop during speech after this many seconds")
    parser.add_argument("--voice-stop", action="store_true", help="Stop only by recognized voice command")
    parser.add_argument("--audio", type=Path, help="Use a custom microphone playback fixture")
    args = parser.parse_args()
    if args.voice_stop and (args.audio is None or args.stop_after is not None):
        parser.error("--voice-stop requires --audio and cannot be combined with --stop-after")
    executable = str(Path(args.executable).resolve())
    root = Path(__file__).resolve().parents[1]
    audio_path = args.audio.resolve() if args.audio else root / "tests/fixtures/example.wav"
    audio_metadata = json.loads(audio_path.with_suffix(".json").read_text()) if args.voice_stop else {}
    application = QApplication([])
    application.setQuitOnLastWindowClosed(False)
    target = QPlainTextEdit()
    target.setWindowTitle("Giga Dictation · сквозная проверка запуска")
    target.resize(900, 350)
    result = {}
    process = player = module = None
    terminal = None
    original_source = subprocess.check_output(["pactl", "get-default-source"], text=True).strip()
    with tempfile.TemporaryDirectory(prefix="e2e-", dir=root / ".tools") as temporary:
        profile = Path(temporary)
        shutil.copytree(
            root / "models",
            profile / "models",
            copy_function=os.link,
            ignore=shutil.ignore_patterns(".cache"),
        )
        with socket.socket() as free_port:
            free_port.bind(("127.0.0.1", 0))
            port = free_port.getsockname()[1]
        settings = Settings(
            server_url=f"ws://127.0.0.1:{port}/v1/dictate",
            load_on_demand=args.on_demand,
            stop_on_phrase=args.voice_stop,
        )
        settings.save(profile)
        environment = dict(os.environ, GIGA_DICTATION_HOME=str(profile), HF_HUB_OFFLINE="1")
        began = time.monotonic()
        speech_start = stopped_at = first_insert = None
        triggered_at = playback_at = None
        error = ""
        receiver_output = profile / "terminal-input.txt"
        terminal_target = ""
        finished_sessions = []
        inserted_prefix = ""

        def memory_mib(pid):
            try:
                fields = Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines()
                return int(next(line.split()[1] for line in fields if line.startswith("Pss:"))) / 1024
            except (FileNotFoundError, StopIteration):
                return 0

        def startup_metrics():
            path = profile / ".data/app.log"
            if not path.exists():
                return {}
            lines = [line for line in path.read_text().splitlines() if "Модель готова: " in line]
            return json.loads(lines[-1].split("Модель готова: ", 1)[1]) if lines else {}

        def press():
            connection = display.Display()
            before = connection.screen().root.query_pointer().mask
            chord = "+".join(part.strip("<>") for part in settings.hotkey.split("+"))
            subprocess.run(["xdotool", "key", chord], check=True)
            result.setdefault("hotkey_modifier_masks", []).append(
                {"before": before, "after": connection.screen().root.query_pointer().mask}
            )
            connection.close()

        def play():
            nonlocal player, triggered_at
            triggered_at = time.monotonic()
            press()
            QTimer.singleShot(800, lambda: start_player())

        def start_player():
            nonlocal player, playback_at
            playback_at = time.monotonic()
            player = subprocess.Popen(["paplay", "--device=giga_dictation_test", str(audio_path)])

        def poll():
            nonlocal speech_start, first_insert, stopped_at, error, terminal, terminal_target
            nonlocal inserted_prefix, triggered_at, playback_at, player
            try:
                if process.poll() is not None:
                    raise RuntimeError(
                        "Приложение завершилось до проверки: "
                        + (profile / "launcher.log").read_text(errors="replace")
                    )
                if speech_start is None:
                    if args.on_demand:
                        windows = subprocess.run(
                            ["xdotool", "search", "--onlyvisible", "--name", "^Giga Dictation$"],
                            capture_output=True,
                            text=True,
                        )
                        ready = windows.returncode == 0
                        if ready:
                            try:
                                health(settings.server_url)
                            except OSError:
                                result["server_absent_before_activation"] = True
                            else:
                                raise AssertionError("Модель запущена до хоткея в режиме по требованию")
                    else:
                        try:
                            ready = health(settings.server_url).get("ready")
                        except OSError:
                            ready = False
                    if not ready:
                        if time.monotonic() - began > 120:
                            raise RuntimeError("Локальный сервер не запустился")
                        return
                    if args.terminal:
                        if terminal is None:
                            terminal = subprocess.Popen(
                                [
                                    "xfce4-terminal",
                                    "--disable-server",
                                    "--title=Giga Dictation full terminal test",
                                    "--dynamic-title-mode=none",
                                    "--execute",
                                    sys.executable,
                                    str(root / "tests/support/terminal_receiver.py"),
                                    "--output",
                                    str(receiver_output),
                                    "--bracketed",
                                ]
                            )
                            return
                        if not receiver_output.with_suffix(".ready").exists():
                            return
                        terminal_target = (
                            subprocess.check_output(
                                [
                                    "xdotool",
                                    "search",
                                    "--onlyvisible",
                                    "--name",
                                    "Giga Dictation full terminal test",
                                ],
                                text=True,
                            )
                            .strip()
                            .splitlines()[-1]
                        )
                        target_id = terminal_target
                    else:
                        target.show()
                        target.raise_()
                        target.activateWindow()
                        target.setFocus()
                        target_id = str(int(target.winId()))
                    subprocess.run(
                        ["xdotool", "windowactivate", "--sync", target_id],
                        check=True,
                        timeout=3,
                    )
                    result["cold_start_seconds"] = time.monotonic() - began
                    speech_start = time.monotonic()
                    # The app's readiness timer polls every two seconds. Wait for its
                    # Record button/hotkey to be enabled after HTTP readiness.
                    result["idle_gui_pss_mib"] = memory_mib(process.pid)
                    QTimer.singleShot(500 if args.on_demand else 2500, play)
                    return
                if triggered_at is not None and "active_server_pss_mib" not in result:
                    children = Path(f"/proc/{process.pid}/task/{process.pid}/children").read_text().split()
                    if children and startup_metrics():
                        result["active_server_pss_mib"] = memory_mib(children[-1])
                current_input = (
                    (receiver_output.read_text() if receiver_output.exists() else "")
                    if args.terminal
                    else target.toPlainText()
                )
                if len(current_input) > len(inserted_prefix) and first_insert is None:
                    first_insert = time.monotonic() - triggered_at
                    result["first_text_after_audio_seconds"] = time.monotonic() - playback_at
                    if not args.terminal:
                        target.grab().save("benchmarks/packaged-running.png")
                if args.voice_stop and playback_at is not None and stopped_at is None:
                    log_path = profile / ".data/app.log"
                    commands = log_path.read_text().count("Диктовка остановлена голосовой командой")
                    if commands > len(finished_sessions):
                        stopped_at = time.monotonic()
                        result["voice_stop_detected"] = True
                        result["voice_stop_latency_seconds"] = (
                            stopped_at - playback_at - audio_metadata["command_end_seconds"]
                        )
                        assert len(result["hotkey_modifier_masks"]) == len(finished_sessions) + 1
                    elif player is not None and player.poll() is not None:
                        raise AssertionError("Голосовая команда не завершила запись")
                if (
                    not args.voice_stop
                    and player is not None
                    and player.poll() is not None
                    and stopped_at is None
                ):
                    press()
                    stopped_at = time.monotonic()
                if (
                    args.stop_after is not None
                    and playback_at is not None
                    and stopped_at is None
                    and time.monotonic() - playback_at >= args.stop_after
                ):
                    player.terminate()
                    player.wait(timeout=3)
                    press()
                    stopped_at = time.monotonic()
                if stopped_at is not None and time.monotonic() - stopped_at > 0.7:
                    history = History(profile)
                    try:
                        rows = history.recent()
                    finally:
                        history.close()
                    if len(rows) > len(finished_sessions):
                        actual, expected = current_input[len(inserted_prefix) :], rows[0][1]
                        clipboard = subprocess.check_output(
                            ["xclip", "-selection", "clipboard", "-o"], text=True, timeout=3
                        )
                        if clipboard != expected or actual != expected:
                            if time.monotonic() - stopped_at < 5:
                                return  # final insertion/clipboard publication is still pending
                        if args.on_demand:
                            try:
                                health(settings.server_url)
                            except OSError:
                                result["server_unloaded_after_stop"] = True
                            else:
                                if time.monotonic() - stopped_at < 5:
                                    return
                                raise AssertionError("Модель не выгрузилась после остановки")
                        result.update(
                            executable=executable,
                            recognized_text=expected,
                            inserted_text=actual,
                            matches=actual == expected,
                            first_insert_seconds=first_insert,
                            elapsed_seconds=time.monotonic() - speech_start,
                            audio_source="PortAudio/PulseAudio monitor",
                            activation="global hotkey",
                            hotkey=settings.hotkey,
                            offline=True,
                            target="XFCE Terminal" if args.terminal else "Qt text field",
                            clipboard_text=clipboard,
                            clipboard_matches=clipboard == expected,
                            bracketed_paste=args.terminal,
                            load_on_demand=args.on_demand,
                            stop_on_phrase=args.voice_stop,
                            startup=startup_metrics(),
                            stop_to_result_seconds=time.monotonic() - stopped_at,
                            idle_gui_after_stop_pss_mib=memory_mib(process.pid),
                            first_text_before_stop=first_insert is not None
                            and triggered_at + first_insert < stopped_at,
                        )
                        assert actual == expected and actual, result
                        assert clipboard == expected, result
                        if args.voice_stop:
                            assert "конец связи" not in expected.lower(), expected
                            assert expected.lower().count("ничьих") == 1, expected
                            assert 0 <= result["voice_stop_latency_seconds"] < 5, result
                        if args.stop_after is None:
                            assert expected.lower().startswith("ничьих"), expected
                            assert "лукоморья" in expected.lower(), expected
                            assert result["first_text_before_stop"], result
                        else:
                            # A truncated one-second utterance can change spelling;
                            # verify that its opening word survived, not its WER.
                            assert expected.lower().startswith("нич"), expected
                        assert first_insert is not None and first_insert < (8 if args.on_demand else 6), (
                            result
                        )
                        finished_sessions.append(dict(result))
                        if len(finished_sessions) < args.sessions:
                            inserted_prefix = current_input
                            first_insert = stopped_at = triggered_at = playback_at = player = None
                            speech_start = time.monotonic()
                            QTimer.singleShot(300, play)
                            return
                        timer.stop()
                        application.quit()
                if time.monotonic() - speech_start > 60:
                    raise RuntimeError("Запись не завершилась или не сохранилась в историю")
            except Exception as exception:
                error = str(exception)
                timer.stop()
                application.quit()

        try:
            module = subprocess.check_output(
                ["pactl", "load-module", "module-null-sink", "sink_name=giga_dictation_test"], text=True
            ).strip()
            subprocess.run(["pactl", "set-default-source", "giga_dictation_test.monitor"], check=True)
            with (profile / "launcher.log").open("w") as log:
                process = subprocess.Popen([executable], env=environment, stdout=log, stderr=log)
                timer = QTimer()
                timer.timeout.connect(poll)
                timer.start(100)
                application.exec()
                if error:
                    print(error, flush=True)
                if process.poll() is None:
                    subprocess.run([executable, "quit"], env=environment, check=True, timeout=15)
                process.wait(timeout=15)
                try:
                    health(settings.server_url)
                except OSError:
                    result["owned_server_stopped"] = True
                else:
                    raise AssertionError("Сервер остался запущенным после выхода")
                if error:
                    raise RuntimeError(error)
                assert process.returncode == 0, process.returncode
                result["sessions"] = finished_sessions
                Path(args.output).write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(json.dumps(result, ensure_ascii=False))
        finally:
            logs = []
            for name in ("launcher.log", ".data/server.log", ".data/app.log", "terminal-input.txt"):
                path = profile / name
                if path.exists():
                    logs.append(name + "\n" + path.read_text(errors="replace"))
            (root / "benchmarks/launcher-last.log").write_text("\n".join(logs), encoding="utf-8")
            if process and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            if player and player.poll() is None:
                player.terminate()
                player.wait(timeout=3)
            if terminal and terminal.poll() is None:
                terminal.terminate()
                terminal.wait(timeout=5)
            subprocess.run(["pactl", "set-default-source", original_source], check=True)
            if module is not None:
                subprocess.run(["pactl", "unload-module", module], check=True)


if __name__ == "__main__":
    main()
